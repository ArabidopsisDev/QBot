import io
import unittest
from pathlib import Path

from gate_report_pdf import pdf_page_count, render_page_png, render_pdf
from pypdf import PdfReader


class GateReportPdfTests(unittest.TestCase):
    def report(self):
        return {
            'report_id':'a'*32,'group_id':'312579123','user_id':'800000',
            'occurred_at':1760000000,'received_at':1760000001,
            'nickname':'张同学','primary_gate':'04','labels':['04a'],
            'text':'喵喵喵喵，我想试试这条报告的排版。',
            'findings':[{'gate':'04','category':'04a',
                         'evidence':{'segment':'喵','repeats':4,'run_chars':4}}],
            'suggested_points':0,'suggested_ban_seconds':0,
            'status':'pending','uncertain':False,
        }

    def test_report_has_formal_sections_and_no_json(self):
        pdf=render_pdf(self.report())
        self.assertTrue(pdf.startswith(b'%PDF-'))
        reader=PdfReader(io.BytesIO(pdf))
        text='\n'.join(page.extract_text() for page in reader.pages)
        for phrase in ('门禁触发报告','门禁所示','处理建议','张同学','喵喵喵喵'):
            self.assertIn(phrase,text)
        self.assertIn('待审核',text)
        self.assertNotIn('审核意见：',text)
        self.assertNotIn('"evidence"',text)
        self.assertNotIn('"run_chars"',text)
        self.assertEqual(pdf_page_count(pdf),1)
        self.assertTrue(render_page_png(pdf,1).startswith(b'\x89PNG\r\n\x1a\n'))

    def test_long_original_message_flows_to_multiple_pages(self):
        report=self.report()
        report['text']='这是一段原始发言。'*900
        reader=PdfReader(io.BytesIO(render_pdf(report)))
        self.assertGreaterEqual(len(reader.pages),2)
        self.assertIn('处理建议',reader.pages[-1].extract_text())

    def test_approved_report_has_decision_and_signature(self):
        report=self.report()
        report.update(status='approved',reviewer='审核员',reviewed_at=1760000050,
                      review={'points':1,'ban_seconds':0,'reason':'人工核对'})
        signature=(Path(__file__).parent/'res'/'sign.png').read_bytes()
        pdf=render_pdf(report,signature_bytes=signature)
        text='\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
        self.assertIn('审核员',text)
        self.assertIn('人工核对',text)

    def test_sensitive_report_uses_case_values_without_declaring_a_ban(self):
        report=self.report()
        report.update(primary_gate='02',labels=['02IVb'],mode='enforce',
                      text='这是一条包含示例词汇的发言',
                      suggested_points=12,suggested_ban_seconds=90,
                      findings=[{'gate':'02','category':'02IVb',
                                 'evidence':{'term':'示例词汇','base_grade':'IVb'},
                                 'flags':[]}])
        pdf=render_pdf(report)
        text='\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
        self.assertIn('示例词汇',text)
        self.assertIn('12',text)
        self.assertIn('90',text)
        self.assertIn('待人工审核',text)
        self.assertNotIn('已被封禁',text)


if __name__=='__main__':
    unittest.main()
