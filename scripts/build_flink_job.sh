#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"
mkdir -p var/flink-build/lib
docker compose cp flink-jobmanager:/opt/flink/lib/flink-dist-1.20.1.jar var/flink-build/lib/flink-dist-1.20.1.jar
docker compose cp kafka:/opt/kafka/libs/kafka-clients-3.8.1.jar var/flink-build/lib/kafka-clients-3.8.1.jar

build_dir="$(mktemp -d "${TMPDIR:-/tmp}/credit-flink-build.XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT
javac --release 17 \
  -cp "var/flink-build/lib/flink-dist-1.20.1.jar:var/flink-build/lib/kafka-clients-3.8.1.jar" \
  -d "$build_dir" credit/flink/RealtimeAggregateJob.java
(cd "$build_dir" && jar xf "$project_root/var/flink-build/lib/kafka-clients-3.8.1.jar")
jar --create --file var/flink-job.jar --main-class credit.flink.RealtimeAggregateJob -C "$build_dir" .
echo "Built var/flink-job.jar"
