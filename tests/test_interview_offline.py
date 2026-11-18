from html.parser import HTMLParser

from scripts.build_interview_offline import extract_questions, render_page


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.question_cards = 0
        self.external_resources = []
        self.search_labels = []
        self.answer = []
        self.summary = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set(attributes.get('class', '').split())
        if tag == 'details' and 'question-card' in classes:
            self.question_cards += 1
        if (tag == 'script' and attributes.get('src')) or (tag == 'link' and attributes.get('href')):
            self.external_resources.append((tag, attributes))
        if attributes.get('id') == 'search':
            self.search_labels.append(attributes.get('aria-label'))
        self.stack.append((tag, classes))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if any('answer' in classes for _, classes in self.stack):
            self.answer.append(data)
        if (any('summary-answer' in classes for _, classes in self.stack)
                and any(tag == 'p' for tag, _ in self.stack)):
            self.summary.append(data)


def test_extract_keeps_question_answer_and_summary_separate():
    source = '''<article class="document"><h1>记录</h1>
    <h2 class="question-title">问题 1：主问题</h2><h3 class="answer-label">回答</h3>
    <p>完整回答 <strong>重点</strong></p><h3 class="summary-label">本题总结</h3><p>短总结</p>
    <h3 class="followup-title">追问 1-A：后续问题</h3><h3 class="answer-label">回答</h3><p>追问回答</p>
    <h3 class="summary-label">本题总结</h3><p>追问总结</p></article>'''
    questions = extract_questions(source)
    assert len(questions) == 2
    assert questions[0]['title'] == '问题 1：主问题'
    assert '<strong>重点</strong>' in questions[0]['answer']
    assert '短总结' not in questions[0]['answer']
    assert questions[0]['summary'].strip() == '<p>短总结</p>'
    assert questions[1]['kind'] == '追问'
    assert questions[1]['answer'].strip() == '<p>追问回答</p>'


def test_render_is_offline_and_preserves_question_content():
    question = {'id': 'sec-2', 'title': '问题 1：测试', 'kind': '主问题', 'answer': '<p>完整回答</p>', 'summary': '<p>总结</p>'}
    page = render_page([question])
    parser = PageParser()
    parser.feed(page)
    assert parser.question_cards == 1
    assert ''.join(parser.answer).strip() == '完整回答'
    assert ''.join(parser.summary).strip() == '总结'
    assert not parser.external_resources
    assert parser.search_labels == ['搜索题目']
