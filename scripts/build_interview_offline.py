"""Build a standalone interview practice page from the saved Q&A record."""
from pathlib import Path
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'docs/reference/B 端指标问答与报告 Agent.html'
OUTPUT = ROOT / 'docs/reference/B 端指标问答与报告 Agent-离线题库.html'


def extract_questions(source):
    soup = BeautifulSoup(source, 'html.parser')
    article = soup.select_one('article.document')
    if article is None:
        raise ValueError('找不到问答正文 article.document')
    questions = []
    current = None
    section = None
    for node in article.find_all(recursive=False):
        classes = node.get('class', [])
        if 'question-title' in classes or 'followup-title' in classes:
            if current:
                questions.append(current)
            current = {'id': node.get('id', f'question-{len(questions)+1}'),
                       'title': node.get_text(' ', strip=True),
                       'kind': '追问' if 'followup-title' in classes else '主问题',
                       'answer': '', 'summary': ''}
            section = 'answer'
        elif current:
            if 'answer-label' in classes:
                section = 'answer'
            elif 'summary-label' in classes:
                section = 'summary'
            elif node.name == 'h2':
                questions.append(current)
                current = None
                section = None
            elif section:
                for unsafe in node.select('script, iframe, object, embed'):
                    unsafe.decompose()
                for tag in [node, *node.find_all(True)]:
                    for attr in list(tag.attrs):
                        if attr.lower().startswith('on'):
                            del tag[attr]
                current[section] += str(node) + '\n'
    if current:
        questions.append(current)
    return questions


def render_page(questions):
    soup = BeautifulSoup('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>B 端指标问答与报告 Agent｜离线面试题库</title></head><body></body></html>', 'html.parser')
    style = soup.new_tag('style')
    style.string = '''*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f2f5f9;color:#263448;font:16px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}header{background:#19314f;color:#fff;padding:26px max(20px,calc((100vw - 1100px)/2))}h1{font-size:27px;line-height:1.3;margin:0 0 7px}header p{margin:0;color:#cbd9e8}main{max-width:1100px;margin:24px auto;padding:0 18px}.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}input,select,button{font:inherit;border:1px solid #cbd6e2;border-radius:8px;padding:9px 12px;background:#fff}input{flex:1;min-width:240px}button{cursor:pointer}button:hover{border-color:#2b70a7}.stats{color:#60748b;margin:0 0 15px}.question-card{background:#fff;border:1px solid #dce5ee;border-radius:12px;margin:0 0 12px;box-shadow:0 2px 12px #1636570a;overflow:hidden}.question-card>summary{cursor:pointer;padding:17px 20px;font-weight:650;line-height:1.5;list-style:none}.question-card>summary::-webkit-details-marker{display:none}.question-card>summary:before{content:'▸';color:#377cab;margin-right:10px}.question-card[open]>summary:before{content:'▾'}.kind{font-size:12px;background:#edf5fc;color:#2a6e9d;padding:3px 7px;border-radius:20px;margin-right:9px}.kind.followup{background:#f3eefb;color:#7752a3}.content{border-top:1px solid #edf1f5;padding:0 20px 18px}.content h2,.content h3{font-size:18px;margin:22px 0 10px}.content p{margin:12px 0}.content pre{background:#192536;color:#ebf1f7;border-radius:8px;padding:15px;overflow:auto;font-size:13px}.content code{font-family:ui-monospace,Consolas,monospace}.content blockquote{margin:14px 0;padding:8px 15px;background:#f5f8fb;border-left:4px solid #8db2d0}.table-wrap{overflow:auto}.content table{border-collapse:collapse;min-width:500px}.content th,.content td{border:1px solid #dce5ed;padding:6px 9px;vertical-align:top}.content ul,.content ol{padding-left:25px}.summary-answer{background:#f3f8fc;border-left:4px solid #4f8fbd;padding:10px 15px;margin-top:20px}.summary-answer h3{margin:0 0 6px;font-size:15px;color:#30688c}.mark-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}.mark-actions button{font-size:13px}.mark-status{font-size:13px;color:#65778b;padding:9px}.empty{padding:38px;text-align:center;color:#697c90}@media(max-width:600px){header{padding:22px 18px}h1{font-size:22px}.question-card>summary{padding:14px}.content{padding:0 14px 14px}}@media print{header,.toolbar,.stats,.mark-actions{display:none}body{background:#fff}.question-card{box-shadow:none;break-inside:avoid}.question-card>.content{display:block}}'''
    soup.head.append(style)
    header = BeautifulSoup('<header><h1>B 端指标问答与报告 Agent</h1><p>面试题库 · 完整回答与 1–2 分钟总结 · 离线可用</p></header>', 'html.parser')
    soup.body.append(header)
    main = soup.new_tag('main')
    main.append(BeautifulSoup('<div class="toolbar"><input id="search" type="search" placeholder="搜索问题、回答或总结（Ctrl/⌘ + K）" aria-label="搜索题目"><select id="kind-filter" aria-label="题型"><option value="all">全部题型</option><option value="主问题">主问题</option><option value="追问">追问</option></select><select id="mark-filter" aria-label="学习状态"><option value="all">全部状态</option><option value="review">需复习</option><option value="done">已掌握</option></select><button id="toggle-all">展开全部</button></div><p id="stats" class="stats"></p><div id="question-list"></div><p id="empty" class="empty" hidden>没有匹配的题目</p>', 'html.parser'))
    listing = main.select_one('#question-list')
    for q in questions:
        details = soup.new_tag('details', attrs={'class': 'question-card', 'id': q['id'], 'data-kind': q['kind']})
        summary = soup.new_tag('summary')
        badge = soup.new_tag('span', attrs={'class': 'kind followup' if q['kind'] == '追问' else 'kind'})
        badge.string = q['kind']
        summary.append(badge)
        summary.append(q['title'])
        details.append(summary)
        content = soup.new_tag('div', attrs={'class': 'content'})
        answer = soup.new_tag('div', attrs={'class': 'answer'})
        answer.append(BeautifulSoup(q['answer'], 'html.parser'))
        content.append(answer)
        short = soup.new_tag('div', attrs={'class': 'summary-answer'})
        label = soup.new_tag('h3')
        label.string = '本题 1–2 分钟总结'
        short.append(label)
        short.append(BeautifulSoup(q['summary'], 'html.parser'))
        content.append(short)
        content.append(BeautifulSoup('<div class="mark-actions"><button type="button" data-mark="review">标记需复习</button><button type="button" data-mark="done">标记已掌握</button><button type="button" data-mark="clear">清除标记</button><span class="mark-status"></span></div>', 'html.parser'))
        details.append(content)
        listing.append(details)
    soup.body.append(main)
    script = soup.new_tag('script')
    script.string = '''const cards=[...document.querySelectorAll('.question-card')],search=document.getElementById('search'),kind=document.getElementById('kind-filter'),markFilter=document.getElementById('mark-filter'),stats=document.getElementById('stats');const storageKey='credit-metric-agent-interview-v1';let marks={};try{marks=JSON.parse(localStorage.getItem(storageKey)||'{}')}catch(e){}function update(){const term=search.value.trim().toLocaleLowerCase();let visible=0;cards.forEach(card=>{const status=marks[card.id]||'';const match=(!term||card.textContent.toLocaleLowerCase().includes(term))&&(kind.value==='all'||card.dataset.kind===kind.value)&&(markFilter.value==='all'||status===markFilter.value);card.hidden=!match;if(match)visible++;card.querySelector('.mark-status').textContent=status==='review'?'需复习':status==='done'?'已掌握':''});stats.textContent=`显示 ${visible} / ${cards.length} 题 · 主问题 ${cards.filter(c=>c.dataset.kind==='主问题').length} 题 · 追问 ${cards.filter(c=>c.dataset.kind==='追问').length} 题 · 需复习 ${Object.values(marks).filter(x=>x==='review').length} 题`;document.getElementById('empty').hidden=visible!==0}for(const el of [search,kind,markFilter])el.addEventListener(el===search?'input':'change',update);document.getElementById('question-list').addEventListener('click',event=>{const btn=event.target.closest('[data-mark]');if(!btn)return;const id=btn.closest('.question-card').id;if(btn.dataset.mark==='clear')delete marks[id];else marks[id]=btn.dataset.mark;try{localStorage.setItem(storageKey,JSON.stringify(marks))}catch(e){}update()});document.getElementById('toggle-all').addEventListener('click',event=>{const visible=cards.filter(c=>!c.hidden);const open=visible.some(c=>!c.open);visible.forEach(c=>c.open=open);event.target.textContent=open?'收起全部':'展开全部'});document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();search.focus()}});update();'''
    soup.body.append(script)
    return str(soup)


def main():
    questions = extract_questions(SOURCE.read_text(encoding='utf-8'))
    OUTPUT.write_text(render_page(questions), encoding='utf-8')
    print(f'生成 {len(questions)} 题：{OUTPUT}')


if __name__ == '__main__':
    main()
