import json
import os
import unittest
from unittest.mock import patch, Mock
from mail_summary import fallback_structured_summary, validate_structured_summary, structured_summary_with_ollama


class StructuredSummaryTest(unittest.TestCase):
    def test_fallback_has_four_fields(self):
        text = 'ERP 전환 계획 확인 요청드립니다. 11월 25일까지 검토 후 공유해 주세요.'
        s = fallback_structured_summary('ERP 전환', text)
        self.assertEqual(set(s), {'main', 'request', 'schedule', 'action'})
        self.assertIn('ERP', s['main'])

    def test_invalid_json_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_structured_summary('일정 미정')
        with self.assertRaises(ValueError):
            validate_structured_summary({'main': 'x'})

    def test_valid_response(self):
        fields = {'main':'ERP 일정 변경 안내', 'request':'확인 요청', 'schedule':'25일까지', 'action':'일정 공유'}
        response = Mock()
        response.json.return_value={'message':{'content':json.dumps(fields, ensure_ascii=False)}}
        response.raise_for_status.return_value=None
        with patch.dict(os.environ, {'MAIL_SUMMARY_AI':'true','MAIL_SUMMARY_MODEL':'qwen3.5:4b'}), patch('requests.post', return_value=response) as post:
            result=structured_summary_with_ollama('일정', '변경 일정을 확인 요청드립니다.')
        self.assertEqual(result['status'],'success')
        self.assertEqual(result['model'],'qwen3.5:4b')
        self.assertEqual(result['summary'],fields)
        self.assertEqual(post.call_args.kwargs['json']['model'],'qwen3.5:4b')

    def test_model_error_falls_back_without_false_success(self):
        with patch.dict(os.environ, {'MAIL_SUMMARY_AI':'true','MAIL_SUMMARY_MODEL':'missing-model'}), patch('requests.post', side_effect=TimeoutError('time out')):
            result=structured_summary_with_ollama('확인 요청', 'ERP 일정 확인 요청드립니다.')
        self.assertEqual(result['status'],'fallback')
        self.assertEqual(result['model'],'missing-model')
        self.assertEqual(len(result['summary']),4)

    def test_disabled(self):
        with patch.dict(os.environ, {'MAIL_SUMMARY_AI':'false'}):
            self.assertEqual(structured_summary_with_ollama('제목','본문 내용입니다.')['status'],'disabled')


if __name__=='__main__': unittest.main()

class FallbackQualityRegression(unittest.TestCase):
    def test_apology_not_reused_in_all_fields(self):
        s=fallback_structured_summary('내서버돌보미 11월 정보보호교육 안내',
            '회신 기한 안에 회신하지 못한 점 양해 부탁드립니다.\n'
            '11/19(목) 17:00까지 정보보호교육 신청을 바랍니다.\n'
            '신청 방법은 3.참고 내용을 확인해 주세요.')
        self.assertNotIn('회신 기한 안에 회신하지 못한', s['request'])
        self.assertNotIn('회신 기한 안에 회신하지 못한', s['schedule'])
        self.assertNotIn('회신 기한 안에 회신하지 못한', s['action'])
        self.assertIn('11/19',s['schedule'])

    def test_schedule_without_date_is_missing(self):
        s=fallback_structured_summary('업무 일정 협의', '업무 일정을 검토해 주세요. 조율 후 공유하겠습니다.')
        self.assertEqual(s['schedule'],'메일에 명시되지 않음')
