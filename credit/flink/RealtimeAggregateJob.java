package credit.flink;

import java.io.Serializable;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.LocalDate;
import java.util.Base64;
import java.util.List;
import java.util.Properties;

import org.apache.flink.api.common.functions.MapFunction;
import org.apache.flink.api.common.state.ListState;
import org.apache.flink.api.common.state.ListStateDescriptor;
import org.apache.flink.api.common.state.ValueState;
import org.apache.flink.api.common.state.ValueStateDescriptor;
import org.apache.flink.configuration.Configuration;
import org.apache.flink.runtime.state.FunctionInitializationContext;
import org.apache.flink.runtime.state.FunctionSnapshotContext;
import org.apache.flink.streaming.api.CheckpointingMode;
import org.apache.flink.streaming.api.checkpoint.CheckpointedFunction;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.functions.KeyedProcessFunction;
import org.apache.flink.streaming.api.functions.sink.RichSinkFunction;
import org.apache.flink.streaming.api.functions.source.SourceFunction;
import org.apache.flink.util.Collector;
import org.apache.flink.shaded.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.apache.flink.shaded.jackson2.com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.flink.shaded.jackson2.com.fasterxml.jackson.databind.node.ObjectNode;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.consumer.ConsumerRecords;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.common.TopicPartition;

/** One-partition local demonstration: Kafka loan changes -> Flink state -> Doris REALTIME aggregate. */
public final class RealtimeAggregateJob {
    private static final ObjectMapper JSON = new ObjectMapper();

    private RealtimeAggregateJob() {}

    public static void main(String[] args) throws Exception {
        String bootstrap = setting(args, 0, "CREDIT_KAFKA_INTERNAL", "kafka:29092");
        String topic = setting(args, 1, "CREDIT_KAFKA_TOPIC", "credit.loan-events.v1");
        String doris = setting(args, 2, "CREDIT_DORIS_STREAM_LOAD",
                "http://doris:8040/api/credit_demo/realtime_aggregate/_stream_load");

        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        env.setParallelism(1);
        env.enableCheckpointing(10_000L, CheckpointingMode.EXACTLY_ONCE);
        DataStream<Event> events = env.addSource(new PartitionZeroSource(bootstrap, topic))
                .name("Kafka partition 0, checkpointed offset")
                .map((MapFunction<String, Event>) Event::parse).name("Validate LoanEvent JSON");
        DataStream<Delta> changes = events.keyBy(event -> event.biz_date + "|" + event.loan_id)
                .process(new LoanChanges()).name("Ignore duplicate and late loan versions");
        changes.keyBy(delta -> delta.biz_date + "|" + delta.product + "|" + delta.domain)
                .process(new GroupTotals()).name("Update day-product-domain totals")
                .addSink(new DorisStreamLoad(doris)).name("Doris REALTIME unique-key stream load");
        env.execute("credit-loan-realtime-aggregate");
    }

    private static String setting(String[] args, int index, String env, String fallback) {
        if (args.length > index && !args[index].isBlank()) return args[index];
        String value = System.getenv(env);
        return value == null || value.isBlank() ? fallback : value;
    }

    /** The demo topic is explicitly one partition; Flink owns its offset in checkpoint state. */
    public static final class PartitionZeroSource implements SourceFunction<String>, CheckpointedFunction {
        private final String bootstrap;
        private final String topic;
        private transient ListState<Long> offsets;
        private volatile boolean running = true;
        private long nextOffset = -1;

        public PartitionZeroSource(String bootstrap, String topic) {
            this.bootstrap = bootstrap;
            this.topic = topic;
        }

        @Override
        public void initializeState(FunctionInitializationContext context) throws Exception {
            offsets = context.getOperatorStateStore().getListState(new ListStateDescriptor<>("next-kafka-offset", Long.class));
            if (context.isRestored()) {
                for (Long saved : offsets.get()) nextOffset = saved;
            }
        }

        @Override
        public void snapshotState(FunctionSnapshotContext context) throws Exception {
            offsets.clear();
            offsets.add(nextOffset);
        }

        @Override
        public void run(SourceContext<String> context) {
            Properties config = new Properties();
            config.put("bootstrap.servers", bootstrap);
            config.put("key.deserializer", "org.apache.kafka.common.serialization.StringDeserializer");
            config.put("value.deserializer", "org.apache.kafka.common.serialization.StringDeserializer");
            config.put("enable.auto.commit", "false");
            config.put("client.id", "credit-flink-local");
            TopicPartition partition = new TopicPartition(topic, 0);
            try (KafkaConsumer<String, String> consumer = new KafkaConsumer<>(config)) {
                if (consumer.partitionsFor(topic).size() != 1)
                    throw new IllegalStateException("The local Flink job requires exactly one Kafka partition");
                consumer.assign(List.of(partition));
                if (nextOffset >= 0) consumer.seek(partition, nextOffset);
                else consumer.seekToBeginning(List.of(partition));
                while (running) {
                    ConsumerRecords<String, String> records = consumer.poll(Duration.ofMillis(500));
                    for (ConsumerRecord<String, String> record : records) {
                        synchronized (context.getCheckpointLock()) {
                            context.collect(record.partition() + "\t" + record.offset() + "\t" + record.value());
                            nextOffset = record.offset() + 1;
                        }
                    }
                }
            }
        }

        @Override
        public void cancel() { running = false; }
    }

    public static final class Event implements Serializable {
        public String event_id, loan_id, biz_date, product, domain, digest;
        public long version, balance_cents, overdue_cents, kafka_offset;
        public int kafka_partition;

        public Event() {}

        public static Event parse(String message) throws Exception {
            String[] parts = message.split("\t", 3);
            if (parts.length != 3) throw new IllegalArgumentException("Kafka envelope is incomplete");
            JsonNode node = JSON.readTree(parts[2]);
            Event event = new Event();
            event.kafka_partition = Integer.parseInt(parts[0]);
            event.kafka_offset = Long.parseLong(parts[1]);
            event.digest = java.util.HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(parts[2].getBytes(StandardCharsets.UTF_8)));
            event.event_id = string(node, "event_id");
            event.loan_id = string(node, "loan_id");
            event.biz_date = string(node, "biz_date");
            event.product = string(node, "product");
            event.domain = string(node, "domain");
            event.version = positive(node, "version");
            event.balance_cents = nonnegative(node, "balance_cents");
            event.overdue_cents = nonnegative(node, "overdue_cents");
            if (event.event_id.length() > 80 || event.loan_id.length() > 80
                    || !LocalDate.parse(event.biz_date).toString().equals(event.biz_date)
                    || !event.product.matches("[a-z_]{1,30}") || !event.domain.matches("[a-z_]{1,30}")
                    || event.overdue_cents > event.balance_cents || event.kafka_partition != 0) {
                throw new IllegalArgumentException("LoanEvent does not satisfy the certified schema");
            }
            return event;
        }

        private static String string(JsonNode node, String name) {
            JsonNode value = node.get(name);
            if (value == null || !value.isTextual() || value.asText().isBlank())
                throw new IllegalArgumentException("Missing LoanEvent field: " + name);
            return value.asText();
        }

        private static long nonnegative(JsonNode node, String name) {
            JsonNode value = node.get(name);
            if (value == null || !value.isIntegralNumber() || !value.canConvertToLong() || value.longValue() < 0)
                throw new IllegalArgumentException("Invalid LoanEvent number: " + name);
            return value.longValue();
        }

        private static long positive(JsonNode node, String name) {
            long value = nonnegative(node, name);
            if (value == 0) throw new IllegalArgumentException("Invalid LoanEvent version");
            return value;
        }
    }

    public static final class Snapshot implements Serializable {
        public String event_id, digest, product, domain;
        public long version, balance_cents, overdue_cents;
        public Snapshot() {}
    }

    public static final class Delta implements Serializable {
        public String biz_date, product, domain;
        public long balance_cents, overdue_cents, loan_count, kafka_offset;
        public int kafka_partition;
        public Delta() {}

        static Delta of(Event event, String product, String domain, long balance, long overdue, long count) {
            Delta result = new Delta();
            result.biz_date = event.biz_date;
            result.product = product;
            result.domain = domain;
            result.balance_cents = balance;
            result.overdue_cents = overdue;
            result.loan_count = count;
            result.kafka_offset = event.kafka_offset;
            result.kafka_partition = event.kafka_partition;
            return result;
        }
    }

    public static final class LoanChanges extends KeyedProcessFunction<String, Event, Delta> {
        private transient ValueState<Snapshot> snapshots;

        @Override
        public void open(Configuration config) {
            snapshots = getRuntimeContext().getState(new ValueStateDescriptor<>("loan-version", Snapshot.class));
        }

        @Override
        public void processElement(Event event, Context context, Collector<Delta> output) throws Exception {
            Snapshot old = snapshots.value();
            if (old != null) {
                if (old.event_id.equals(event.event_id)) {
                    if (!old.digest.equals(event.digest)) throw new IllegalArgumentException("event_id payload changed");
                    return;
                }
                if (event.version == old.version) throw new IllegalArgumentException("loan version conflict");
                if (event.version < old.version) return;
                if (old.product.equals(event.product) && old.domain.equals(event.domain)) {
                    output.collect(Delta.of(event, event.product, event.domain,
                            event.balance_cents - old.balance_cents,
                            event.overdue_cents - old.overdue_cents, 0));
                } else {
                    output.collect(Delta.of(event, old.product, old.domain,
                            -old.balance_cents, -old.overdue_cents, -1));
                    output.collect(Delta.of(event, event.product, event.domain,
                            event.balance_cents, event.overdue_cents, 1));
                }
            } else {
                output.collect(Delta.of(event, event.product, event.domain,
                        event.balance_cents, event.overdue_cents, 1));
            }
            Snapshot next = new Snapshot();
            next.event_id = event.event_id;
            next.digest = event.digest;
            next.version = event.version;
            next.product = event.product;
            next.domain = event.domain;
            next.balance_cents = event.balance_cents;
            next.overdue_cents = event.overdue_cents;
            snapshots.update(next);
        }
    }

    public static final class Aggregate implements Serializable {
        public String biz_date, product, domain;
        public long loan_balance_cents, overdue_balance_cents, loan_count, kafka_offset;
        public int kafka_partition;
        public Aggregate() {}
    }

    public static final class GroupTotals extends KeyedProcessFunction<String, Delta, Aggregate> {
        private transient ValueState<Aggregate> totals;

        @Override
        public void open(Configuration config) {
            totals = getRuntimeContext().getState(new ValueStateDescriptor<>("group-totals", Aggregate.class));
        }

        @Override
        public void processElement(Delta delta, Context context, Collector<Aggregate> output) throws Exception {
            Aggregate row = totals.value();
            if (row == null) row = new Aggregate();
            row.biz_date = delta.biz_date;
            row.product = delta.product;
            row.domain = delta.domain;
            row.loan_balance_cents = Math.addExact(row.loan_balance_cents, delta.balance_cents);
            row.overdue_balance_cents = Math.addExact(row.overdue_balance_cents, delta.overdue_cents);
            row.loan_count = Math.addExact(row.loan_count, delta.loan_count);
            row.kafka_offset = delta.kafka_offset;
            row.kafka_partition = delta.kafka_partition;
            if (row.loan_balance_cents < 0 || row.overdue_balance_cents < 0 || row.loan_count < 0)
                throw new IllegalStateException("Aggregate became negative");
            totals.update(row);
            output.collect(row);
        }
    }

    public static final class DorisStreamLoad extends RichSinkFunction<Aggregate> {
        private final String endpoint;
        private transient HttpClient client;

        public DorisStreamLoad(String endpoint) { this.endpoint = endpoint; }

        @Override
        public void open(Configuration config) {
            client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3))
                    .version(HttpClient.Version.HTTP_1_1)
                    .followRedirects(HttpClient.Redirect.NEVER).build();
        }

        private URI redirected(URI current, String location) throws Exception {
            URI target = current.resolve(location);
            if ("127.0.0.1".equals(target.getHost()) || "localhost".equals(target.getHost())) {
                // Doris all-in-one advertises its BE as 127.0.0.1:8040. Inside
                // the Flink container that address is Flink itself, not Doris.
                return new URI(target.getScheme(), null, URI.create(endpoint).getHost(),
                        target.getPort(), target.getPath(), target.getQuery(), null);
            }
            return target;
        }

        @Override
        public void invoke(Aggregate row, Context context) throws Exception {
            ObjectNode json = JSON.createObjectNode();
            json.put("biz_date", row.biz_date);
            json.put("product", row.product);
            json.put("domain", row.domain);
            json.put("loan_balance_cents", row.loan_balance_cents);
            json.put("overdue_balance_cents", row.overdue_balance_cents);
            json.put("loan_count", row.loan_count);
            json.put("kafka_offset", row.kafka_offset);
            byte[] payload = JSON.writeValueAsBytes(json);
            String key = row.biz_date + "|" + row.product + "|" + row.domain;
            byte[] hash = MessageDigest.getInstance("SHA-256").digest(key.getBytes(StandardCharsets.UTF_8));
            String label = "credit_rt_" + row.kafka_partition + "_" + row.kafka_offset + "_"
                    + java.util.HexFormat.of().formatHex(hash, 0, 8) + "_"
                    + java.util.UUID.randomUUID().toString().replace("-", "");
            URI url = URI.create(endpoint);
            for (int redirect = 0; redirect < 3; redirect++) {
                HttpRequest request = HttpRequest.newBuilder(url).version(HttpClient.Version.HTTP_1_1)
                        .timeout(Duration.ofSeconds(20))
                        .header("Authorization", "Basic " + Base64.getEncoder()
                                .encodeToString("root:".getBytes(StandardCharsets.UTF_8)))
                        .header("Content-Type", "application/json")
                        .header("format", "json")
                        .header("read_json_by_line", "true")
                        .header("max_filter_ratio", "0")
                        .header("label", label)
                        .PUT(HttpRequest.BodyPublishers.ofByteArray(payload)).build();
                HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
                int status = response.statusCode();
                if (status == 307 || status == 308) {
                    url = redirected(url, response.headers().firstValue("Location").orElseThrow());
                    continue;
                }
                JsonNode answer = JSON.readTree(response.body());
                String result = answer.path("Status").asText();
                if (status == 200 && ("Success".equals(result)
                        || ("Label Already Exists".equals(result)
                        && "FINISHED".equalsIgnoreCase(answer.path("ExistingJobStatus").asText())))) return;
                throw new IllegalStateException("Doris stream load failed: HTTP " + status + " " + answer);
            }
            throw new IllegalStateException("Too many Doris stream load redirects");
        }
    }
}
