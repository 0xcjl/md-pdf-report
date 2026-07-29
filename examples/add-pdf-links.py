"""
v4 流程:
1. MD → HTML → PDF(视觉干净,链接有 hover 下划线)
2. 从 MD 源解析所有 (链接文字, URL) 对
3. 用 pdfminer 找每页的文本块,匹配链接文字,得到 bbox
4. pikepdf 给对应 bbox 加 link annotation
"""
import sys, re
sys.path.insert(0, '/Users/unilin/.hermes/skills/md-pdf-report')
import md2pdf  # bootstrap
from md2pdf import md_to_html, DEFAULT_CSS
from weasyprint import HTML
import pikepdf
from pikepdf import Pdf, Name, Dictionary, Array
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer

SRC_MD = '/tmp/uni-fee-switch-report.md'
TMP_PDF = '/tmp/uni-fee-switch-report-tmp.pdf'
DST_PDF = '/tmp/uni-fee-switch-report-final.pdf'

# Step 1: CSS — 视觉干净,链接有蓝色下划线但 URL 不显示
# 来源:段落加灰色小字 + 左边距,和正文明显区分
LINK_CSS = DEFAULT_CSS + """
a { color: #1D4ED8; text-decoration: underline; text-decoration-color: rgba(29, 78, 216, 0.4); }
/* 任何以"来源:"开头的段落:小字、灰色、左缩进 */
p { line-height: 1.55; }
"""

md_text = open(SRC_MD, encoding='utf-8').read()

# Step 1.5: 从 MD 抽 (link_text, url) 对
# markdown 链接语法:[文字](url)
LINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^)]+)\)')
md_links = LINK_RE.findall(md_text)
print(f'从 MD 抽出 {len(md_links)} 个链接对')
# 不去重:同一 URL 在表格多行出现,要为每一行单独生成 annotation
md_links = LINK_RE.findall(md_text)
print(f'从 MD 抽出 {len(md_links)} 个链接对(含重复)')

# Step 2: HTML → PDF
html = md_to_html(md_text, css=LINK_CSS, title='UNI report')
HTML(string=html).write_pdf(TMP_PDF)
print(f'PDF 已生成: {TMP_PDF}')

# Step 3: pdfminer 抽每页文本块 + bbox,匹配链接文字
# 思路:对每个 (link_text, url),在每页找包含 link_text 的 LTTextContainer
page_layouts = list(extract_pages(TMP_PDF))
print(f'页面数: {len(page_layouts)}')

# 把所有 LTTextContainer 收集出来,按页索引
page_text_blocks = []
for page_layout in page_layouts:
    blocks = []
    for element in page_layout:
        if isinstance(element, LTTextContainer):
            blocks.append(element)
    page_text_blocks.append(blocks)

# 匹配:对每个 (link_text, url),遍历每页,找包含 link_text 的 block
# 一个 link_text 可能跨多个字符(比如英文短横线),用首尾词+中间关键 token 匹配
def normalize(s):
    return re.sub(r'\s+', ' ', s).strip().lower()

# 先按 link_text 建索引,只对 MD 中出现的链接去匹配
total_matched = 0
total_unmatched = []
matched_urls_per_page = [[] for _ in page_layouts]

# 记录每个 block 已被占用的次数(用于跳过重复匹配)
used_blocks = set()  # (page_idx, block_id(block,bbox 字符串))

for link_text, url in md_links:
    target = normalize(link_text)
    fingerprint = target[:25] if len(target) > 25 else target
    found = False
    used_bbox = None
    for page_idx, blocks in enumerate(page_text_blocks):
        for b_idx, block in enumerate(blocks):
            block_key = (page_idx, b_idx)
            if block_key in used_blocks:
                continue  # 这个 block 已被前面的链接占了
            block_text = normalize(block.get_text())
            if target and target in block_text:
                used_bbox = block.bbox
                matched_urls_per_page[page_idx].append((url, used_bbox, link_text))
                used_blocks.add(block_key)
                found = True
                break
        if found:
            break
    if not found:
        # fingerprint 兜底,也不占用 block(允许后续链接再用)
        for page_idx, blocks in enumerate(page_text_blocks):
            for b_idx, block in enumerate(blocks):
                block_key = (page_idx, b_idx)
                if block_key in used_blocks:
                    continue
                block_text = normalize(block.get_text())
                if fingerprint and fingerprint in block_text:
                    matched_urls_per_page[page_idx].append((url, block.bbox, link_text))
                    used_blocks.add(block_key)
                    found = True
                    break
            if found:
                break
    if found:
        total_matched += 1
    else:
        total_unmatched.append((link_text, url))

print(f'匹配成功: {total_matched}/{len(md_links)}')
if total_unmatched:
    print(f'未匹配示例(前 5):')
    for lt, u in total_unmatched[:5]:
        print(f'  - [{lt}]({u})')

# Step 4: pikepdf 加 annotation
pdf = Pdf.open(TMP_PDF)
total_added = 0
for i, page in enumerate(pdf.pages):
    if i >= len(matched_urls_per_page):
        continue
    matches = matched_urls_per_page[i]
    if not matches:
        continue
    annots = page.get('/Annots', Array([]))
    for url, (x0, y0, x1, y1), link_text in matches:
        # 扩大 Rect 边距,确保点击区域足够大
        # pdfminer 的 bbox 在表格里可能偏小,加 padding 让命中更容易
        pad = 2
        # 表格里 x0/x1 已经接近列边界,横向也加 padding
        if x1 - x0 < 150:  # 较窄区域(可能是表格列)
            pad_h = 4
        else:
            pad_h = 2
        rect_x0 = float(x0) - pad_h
        rect_y0 = float(y0) - pad
        rect_x1 = float(x1) + pad_h
        rect_y1 = float(y1) + pad
        annot = Dictionary(
            Type=Name('/Annot'),
            Subtype=Name('/Link'),
            Rect=Array([rect_x0, rect_y0, rect_x1, rect_y1]),
            Border=Array([0, 0, 0]),
            A=Dictionary(Type=Name('/Action'), S=Name('/URI'), URI=url)
        )
        annots.append(annot)
        total_added += 1
    page['/Annots'] = annots

pdf.save(DST_PDF)
print(f'Final PDF: {DST_PDF}')
print(f'加 annotation: {total_added}')