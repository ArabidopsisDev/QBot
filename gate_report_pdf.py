"""Render a gate review as a formal A4 PDF, without exposing JSON or temp files."""
from __future__ import annotations

import html
import io
import re
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent
PDF_PACKAGES=ROOT/'vendor'/'pdf-env'/'Lib'/'site-packages'
if PDF_PACKAGES.is_dir() and str(PDF_PACKAGES) not in sys.path:
    sys.path.insert(0,str(PDF_PACKAGES))

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (Flowable, HRFlowable, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)
import pymupdf

FONT_ROOT=Path('C:/Windows/Fonts')
INK=colors.HexColor('#111111')
_FONTS_READY=False


def _fonts():
    global _FONTS_READY
    if _FONTS_READY:
        return
    pdfmetrics.registerFont(TTFont('QBotSong',str(FONT_ROOT/'simsun.ttc'),subfontIndex=0))
    pdfmetrics.registerFont(TTFont('QBotHei',str(FONT_ROOT/'simhei.ttf')))
    pdfmetrics.registerFont(TTFont('QBotTimes',str(FONT_ROOT/'times.ttf')))
    # The report title uses the user's corrected choice: 华文中宋.
    candidates=[FONT_ROOT/'STZHONGS.TTF']
    candidates.append(FONT_ROOT/'simsunb.ttf')
    for path in candidates:
        if not path.is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont('QBotTitle',str(path)))
            break
        except Exception:
            continue
    else:
        pdfmetrics.registerFont(TTFont('QBotTitle',str(FONT_ROOT/'simsun.ttc'),
                                        subfontIndex=0))
    _FONTS_READY=True


def _safe(value):
    return html.escape(str(value if value is not None else ''),quote=False)


_LATIN=re.compile(r'([A-Za-z0-9][A-Za-z0-9@._:/?&=%+\-#]*)')


def _mixed(value):
    """Use Song for Chinese and Times New Roman for Latin/digit runs."""
    text=str(value if value is not None else '').replace('\t','    ')
    parts=[]
    for chunk in _LATIN.split(text):
        if not chunk:
            continue
        encoded=_safe(chunk).replace(' ','&#160;')
        if _LATIN.fullmatch(chunk):
            parts.append('<font name="QBotTimes">'+encoded+'</font>')
        else:
            parts.append(encoded)
    return ''.join(parts).replace('\n','<br/>')


def _when(stamp):
    from datetime import datetime
    try:
        return datetime.fromtimestamp(float(stamp)).strftime('%Y-%m-%d %H:%M')
    except (ValueError,TypeError,OverflowError,OSError):
        return '—'


def _styles():
    return {
        'section':ParagraphStyle('section',fontName='QBotHei',fontSize=16,
            leading=23,textColor=INK,spaceBefore=0,spaceAfter=0,
            keepWithNext=True),
        'body':ParagraphStyle('body',fontName='QBotSong',fontSize=14,
            leading=22,textColor=INK,splitLongWords=True),
        'narrative':ParagraphStyle('narrative',fontName='QBotSong',fontSize=14,
            leading=24,textColor=INK,firstLineIndent=28,
            spaceAfter=9,splitLongWords=True),
        'meta':ParagraphStyle('meta',fontName='QBotSong',fontSize=14,
            leading=20,textColor=INK,splitLongWords=True),
        'item':ParagraphStyle('item',fontName='QBotSong',fontSize=14,
            leading=22,textColor=INK,leftIndent=48,
            firstLineIndent=-20,spaceAfter=4,splitLongWords=True),
    }


def _p(text,style):
    return Paragraph(_mixed(text),style)


def _meta(label,value,style):
    return Paragraph('<font name="QBotHei">'+_safe(label)+'：</font>'+_mixed(value),style)


def _section(title,styles):
    return Paragraph(_safe(title),styles['section'])


def _serial(report):
    from datetime import datetime
    try:
        date=datetime.fromtimestamp(float(report.get('occurred_at'))).strftime('%Y%m%d')
    except (TypeError,ValueError,OverflowError,OSError):
        date='00000000'
    return date+'-'+str(report.get('report_id',''))[:8].upper()


class _Title(Flowable):
    """The user's 华文中宋 title, with a restrained synthetic bold stroke."""
    def wrap(self, available_width, available_height):
        self.width=available_width
        self.height=29
        return available_width,self.height

    def draw(self):
        title='门禁触发报告'
        width=pdfmetrics.stringWidth(title,'QBotTitle',20)
        canvas=self.canv
        canvas.saveState()
        canvas.setFillColor(INK)
        canvas.setStrokeColor(INK)
        canvas.setLineWidth(0.35)
        text=canvas.beginText()
        text.setTextOrigin((self.width-width)/2,4)
        text.setFont('QBotTitle',20)
        text.setTextRenderMode(2)
        text.textOut(title)
        canvas.drawText(text)
        canvas.restoreState()


def _observation_paragraphs(report):
    findings=report.get('findings') or []
    original=str(report.get('text') or '').strip()
    person=str(report.get('user_id') or '未知')
    group=str(report.get('group_id') or '未知')
    when=_when(report.get('occurred_at'))
    if original:
        ending='' if original.endswith(('。','！','？','!','?')) else '。'
        first=f'原始 QQ 号：{person}，于 {when} 在群聊 {group} 发送消息“{original}”{ending}'
    else:
        first=f'原始 QQ 号：{person}，于 {when} 在群聊 {group} 发送无文字消息。'
    paragraphs=[first]
    for finding in findings:
        gate=finding.get('gate')
        category=str(finding.get('category') or '未分类')
        evidence=finding.get('evidence') or {}
        if gate=='01':
            if category=='01s':
                merged=evidence.get('merged_length','未知')
                sentence=f'该用户近期多条短消息合计 {merged} 字，符合门禁 {category} 的分拆嫌疑条件，需人工核实是否有规避长消息限制的意图。'
            else:
                sentence=f'该消息共 {evidence.get("code_points","未知")} 字，命中长消息门禁 {category}。'
        elif gate=='02':
            term=str(evidence.get('term') or '未记录')
            grade=str(evidence.get('base_grade') or category[2:])
            sentence=f'上述发言检出词汇「{term}」，与门禁 02 的 {grade} 级词面规则匹配，形成 {category} 待审记录。'
            if finding.get('flags') or report.get('uncertain'):
                sentence+=' 该词可能出现在引用、昵称或其他语境中，需结合原文复核。'
        elif gate=='03':
            if category=='03a':
                sentence=f'该消息含连续英文 {evidence.get("longest_run","未知")} 字母，达到长英文候选规则 {category}。'
            elif category=='03b':
                sentence=f'该消息含长度为 {evidence.get("url_length","未知")} 字符的网址，达到规则 {category}。'
            else:
                sentence=f'该消息含 {evidence.get("distinct","多")} 个不同网址，达到规则 {category}。'
        elif gate=='04':
            sentence=(f'该消息中片段「{evidence.get("segment","未记录")}」连续出现 '
                      f'{evidence.get("repeats","未知")} 次，匹配门禁 {category}。')
        elif gate=='05':
            sentence=(f'该用户最近五条消息均含关键词「{evidence.get("keyword","未记录")}」，'
                      f'其中 {evidence.get("cluster_size","未知")} 条近似重复，形成门禁 {category} 候选记录。')
        elif gate=='06':
            prefix=evidence.get('prefix',evidence.get('symbol','未记录'))
            sentence=(f'该用户此前连续发送相似训练消息 {evidence.get("repeats","未知")} 次，'
                      f'随后单独发送前缀「{prefix}」，形成门禁 {category} 测试行为候选记录。')
        elif gate=='07':
            if category=='07a':
                sentence=(f'在 {evidence.get("window","未知")} 秒内，该用户发送 '
                          f'{evidence.get("messages","未知")} 条消息，达到频率规则 {category}。')
            else:
                sentence=(f'在 {evidence.get("window","未知")} 秒内，该用户发送 '
                          f'{evidence.get("images","未知")} 张图片，达到频率规则 {category}。')
        else:
            sentence=f'该消息形成门禁 {category} 候选记录，需核对原始证据。'
        paragraphs.append(sentence)
    return paragraphs


def _recommendation_items(report):
    review=report.get('review') if report.get('status')=='approved' else None
    if review:
        points=int(review.get('points',0))
        seconds=int(review.get('ban_seconds',0))
        return [f'审核决定：扣除用户质量评分 {points} 分。',
                (f'审核决定：封禁该用户发言 {seconds} 秒。' if seconds else '审核决定：不封禁该用户。'),
                '审核意见：'+str(review.get('reason') or '未填写。')]
    points=int(report.get('suggested_points') or 0)
    seconds=int(report.get('suggested_ban_seconds') or 0)
    first=('本条消息暂不纳入模型训练或回复。' if report.get('mode')=='enforce'
           else '本条消息仅作观察记录，不改变模型处理。')
    return [first,
            (f'建议扣除用户质量评分 {points} 分，待人工审核。' if points else '暂不建议扣分。'),
            (f'建议封禁该用户发言 {seconds} 秒，待人工审核。' if seconds else '暂不建议封禁。')]


class _ReportCanvas(Canvas):
    def __init__(self,*args,report,signature_bytes=None,**kwargs):
        super().__init__(*args,**kwargs)
        self._saved_pages=[]
        self._report=report
        self._signature=signature_bytes

    def showPage(self):
        self._saved_pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total=len(self._saved_pages)
        for index,state in enumerate(self._saved_pages,1):
            self.__dict__.update(state)
            self._draw_footer(index,total)
            super().showPage()
        super().save()

    def _draw_footer(self,index,total):
        width,height=A4
        self.saveState()
        if index==total:
            left,right=24*mm,width-24*mm
            self.setStrokeColor(INK)
            self.setLineWidth(1.5)
            self.line(left,38*mm,right,38*mm)
            self.line(left,25*mm,right,25*mm)
            self.setFont('QBotHei',14)
            self.drawString(left,29*mm,'判定方式：')
            self.setFont('QBotSong',14)
            self.drawString(left+70,29*mm,'自动判定')
            self.setFont('QBotHei',14)
            self.drawString(width-87*mm,29*mm,'审核人：')
            reviewer=(self._report.get('reviewer') or '待审核')
            self.setFont('QBotSong',14)
            self.drawString(width-62*mm,29*mm,str(reviewer)[:16])
            if self._signature and self._report.get('status')=='approved':
                self.drawImage(ImageReader(io.BytesIO(self._signature)),
                               width-68*mm,40*mm,width=42*mm,height=21*mm,
                               preserveAspectRatio=True,anchor='c',mask='auto')
        if total>1:
            self.setFont('QBotTimes',9)
            self.drawRightString(width-24*mm,17*mm,f'{index}/{total}')
        self.restoreState()


def render_pdf(report,*,signature_bytes=None):
    """Return one or more A4 pages as PDF bytes. No filesystem writes."""
    _fonts()
    styles=_styles()
    buffer=io.BytesIO()
    doc=SimpleDocTemplate(buffer,pagesize=A4,rightMargin=23*mm,leftMargin=23*mm,
                          topMargin=16*mm,bottomMargin=64*mm,
                          title='门禁触发报告',author='QBot 本地审核控制台')
    story=[_Title(),Spacer(1,16*mm),
           _meta('触发时间',_when(report.get('occurred_at')),styles['meta']),
           Spacer(1,2*mm),
           HRFlowable(width='100%',thickness=1.5,color=INK,spaceAfter=4*mm)]

    identity=Table([[
        _meta('昵称',report.get('nickname') or '未提供',styles['meta']),
        _meta('QQ 号',report.get('user_id') or '—',styles['meta']),
        _meta('触发类型','自动触发',styles['meta']),
    ]],colWidths=[48*mm,47*mm,69*mm],hAlign='LEFT')
    identity.setStyle(TableStyle([
        ('VALIGN',(0,0),(-1,-1),'TOP'),
        ('LEFTPADDING',(0,0),(-1,-1),0),
        ('RIGHTPADDING',(0,0),(-1,-1),5),
        ('TOPPADDING',(0,0),(-1,-1),0),
        ('BOTTOMPADDING',(0,0),(-1,-1),0),
    ]))
    story += [identity,Spacer(1,1*mm),
              _meta('流水号',_serial(report),styles['meta']),
              Spacer(1,1*mm),
              HRFlowable(width='100%',thickness=1.5,color=INK),
              Spacer(1,10*mm),
              _section('门禁所示：',styles),
              Spacer(1,7*mm)]

    for paragraph in _observation_paragraphs(report):
        story.append(_p(paragraph,styles['narrative']))

    story += [Spacer(1,16*mm),
              _section('处理建议：' if report.get('status')!='approved' else '处理结论：',styles),
              Spacer(1,7*mm)]
    for index,item in enumerate(_recommendation_items(report),1):
        story.append(_p(f'{index}.  {item}',styles['item']))

    doc.build(story,canvasmaker=lambda *args,**kwargs:
              _ReportCanvas(*args,report=report,signature_bytes=signature_bytes,**kwargs))
    return buffer.getvalue()


def pdf_page_count(pdf_bytes):
    with pymupdf.open(stream=pdf_bytes,filetype='pdf') as document:
        return len(document)


def render_page_png(pdf_bytes,page_number,*,scale=1.5):
    """Render exactly one PDF page for browsers without an inline PDF plugin."""
    if type(page_number) is not int or page_number<1:
        raise ValueError('Invalid PDF page')
    with pymupdf.open(stream=pdf_bytes,filetype='pdf') as document:
        if page_number>len(document):
            raise ValueError('PDF page out of range')
        pixmap=document[page_number-1].get_pixmap(
            matrix=pymupdf.Matrix(scale,scale),alpha=False)
        return pixmap.tobytes('png')
