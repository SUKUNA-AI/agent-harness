import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from agent_harness.codex_app_server import NativeCodexEvents
from agent_harness.core import HarnessError

class NativeCodexEventTests(unittest.TestCase):
    def start(self):
        value=NativeCodexEvents('thread-original')
        self.assertEqual(value.feed('turn/started',{'threadId':'thread-original','turn':{'id':'turn-original'}}),[{'type':'turn.started'}])
        return value
    def test_usage_and_mcp_result_are_preserved(self):
        value=self.start()
        value.feed('thread/tokenUsage/updated',{'threadId':'thread-original','turnId':'turn-original','tokenUsage':{'last':{'inputTokens':9,'outputTokens':3,'cachedInputTokens':4,'reasoningOutputTokens':2,'totalTokens':12}}})
        result={'content':[{'type':'text','text':'isolated source'}]}
        events=value.feed('item/completed',{'threadId':'thread-original','turnId':'turn-original','item':{'id':'tool-1','type':'mcpToolCall','server':'serena','tool':'search_symbol','result':result,'error':None}})
        self.assertEqual(events[0]['item']['result'],result)
        self.assertEqual(events[0]['item']['type'],'mcp_tool_call')
        end=value.feed('turn/completed',{'threadId':'thread-original','turn':{'id':'turn-original','status':'completed'}})
        self.assertEqual(end[0]['usage']['input_tokens'],9)
        self.assertEqual(value.finish(),'completed')
    def test_wrong_thread_turn_or_missing_start_never_complete(self):
        for thread,turn in [('new-thread','turn-original'),('thread-original','new-turn')]:
            value=self.start()
            with self.subTest(thread=thread,turn=turn),self.assertRaises(HarnessError):
                value.feed('turn/completed',{'threadId':thread,'turn':{'id':turn,'status':'completed'}})
        with self.assertRaises(HarnessError):
            NativeCodexEvents('thread-original').feed('turn/completed',{'threadId':'thread-original','turn':{'id':'turn-original','status':'completed'}})
    def test_duplicate_terminal_and_late_items_are_invalid(self):
        value=self.start();event={'threadId':'thread-original','turn':{'id':'turn-original','status':'completed'}}
        value.feed('turn/completed',event)
        with self.assertRaises(HarnessError):value.feed('turn/completed',event)
        with self.assertRaises(HarnessError):value.feed('item/completed',{'threadId':'thread-original','item':{'id':'late','type':'agentMessage','text':'late'}})
    def test_unknown_usage_is_not_zero(self):
        value=self.start()
        end=value.feed('turn/completed',{'threadId':'thread-original','turn':{'id':'turn-original','status':'completed'}})
        self.assertEqual(end[0]['usage'],{})
        value=self.start()
        with self.assertRaises(HarnessError):value.feed('thread/tokenUsage/updated',{'threadId':'thread-original','tokenUsage':{'last':{'inputTokens':True}}})
    def test_item_and_usage_require_explicit_active_turn_identity(self):
        for method,fields in [('item/completed',{'item':{'id':'item','type':'agentMessage','text':'invalid'}}),
                ('thread/tokenUsage/updated',{'tokenUsage':{'last':{'inputTokens':1}}})]:
            with self.subTest(method=method),self.assertRaises(HarnessError):
                self.start().feed(method,dict(fields,threadId='thread-original'))
    def test_native_interrupt_is_never_a_failed_or_completed_success_signal(self):
        value=self.start()
        self.assertEqual(value.feed('turn/completed',{'threadId':'thread-original','turn':{'id':'turn-original','status':'interrupted'}}),[{'type':'turn.interrupted'}])
        self.assertEqual(value.finish(),'interrupted')
    def test_malformed_provided_usage_cannot_become_zero_or_known(self):
        value=self.start()
        with self.assertRaises(HarnessError):
            value.feed('thread/tokenUsage/updated',{'threadId':'thread-original','turnId':'turn-original','tokenUsage':{'last':{}}})
    def test_resume_historical_usage_is_validated_but_never_current_coverage(self):
        value=NativeCodexEvents('thread-original',historical_turn_ids=['old-turn'])
        counters={'inputTokens':9,'outputTokens':3,'cachedInputTokens':4,'reasoningOutputTokens':2,'totalTokens':12}
        old={'threadId':'thread-original','turnId':'old-turn','tokenUsage':{'last':counters}}
        self.assertEqual(value.feed('thread/tokenUsage/updated',old),[])
        self.assertIsNone(value.usage)
        with self.assertRaises(HarnessError):
            value.feed('thread/tokenUsage/updated',dict(old,turnId='unobserved-turn'))
        value.feed('turn/started',{'threadId':'thread-original','turn':{'id':'turn-current'}})
        with self.assertRaises(HarnessError):value.feed('thread/tokenUsage/updated',old)
if __name__=='__main__':unittest.main()
