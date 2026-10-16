import json
import unittest
from forgetrace.models import ModelClient
from forgetrace.validation import ContractError


class ModelTests(unittest.TestCase):
    def test_offline_makes_no_transport_calls(self):
        def forbidden(*args): raise AssertionError("Offline called a model")
        client=ModelClient(transport=forbidden)
        self.assertEqual(client.extract([{"content":"fix it"}]),[])
        self.assertEqual(client.embed(["text"]),[])
        self.assertEqual(client.stats()["model_http_attempts"],0)

    def test_exact_quotes_only_and_fixed_model(self):
        seen=[]
        def transport(url,payload):
            seen.append(payload)
            return {"choices":[{"finish_reason":"stop","message":{"content":json.dumps({"anchors":[
                {"snippet_id":0,"kind":"action","quote":"Use guard"},
                {"snippet_id":0,"kind":"cause","quote":"invented explanation"}]})}}],"usage":{"prompt_tokens":12,"completion_tokens":10}}
        client=ModelClient("gpt-4o-mini","dummy-not-a-real-key",transport=transport)
        result=client.extract([{"content":"Use guard before iteration."}])
        self.assertEqual(result[0]["text"],"Use guard")
        self.assertEqual(result[0]["start"],0)
        self.assertEqual(len(result),1)
        self.assertEqual(seen[0]["model"],"gpt-4o-mini")
        self.assertEqual(client.stats()["rejected_anchors"],1)

    def test_malformed_output_is_failure(self):
        client=ModelClient("gpt-4o-mini","dummy-not-a-real-key",transport=lambda *_:{"choices":[]})
        with self.assertRaises(ContractError): client.extract([{"content":"x"}])

    def test_embedding_requires_real_numeric_shape(self):
        client=ModelClient(embedding_mode="v4",embedding_api_key="dummy-not-a-real-key",
            transport=lambda *_:{"data":[{"index":0,"embedding":[1.]*1024}]})
        vector=client.embed(["sample"])[0]
        self.assertAlmostEqual(sum(x*x for x in vector),1.)
        client._transport=lambda *_:{"data":[{"index":0,"embedding":[float("nan")]*1024}]}
        with self.assertRaises(ContractError): client.embed(["sample"])
        client._transport=lambda *_:{"data":[{"index":0,"embedding":[1e308]*1024}]}
        with self.assertRaises(ContractError): client.embed(["sample"])

    def test_request_and_cost_caps_are_enforced_before_transport(self):
        def transport(*_): return {"choices":[{"message":{"content":"{\"anchors\":[]}"}}]}
        client=ModelClient("gpt-4o-mini","dummy-not-a-real-key",transport=transport,max_total_attempts=1,cost_limit_usd=.1)
        client.extract([{"content":"x"}])
        with self.assertRaises(ContractError): client.extract([{"content":"x"}])
        self.assertEqual(client.stats()["model_http_attempts"],1)
        zero=ModelClient("gpt-4o-mini","dummy-not-a-real-key",transport=transport,cost_limit_usd=0.)
        with self.assertRaises(ContractError): zero.extract([{"content":"x"}])
        self.assertEqual(zero.stats()["model_http_attempts"],0)

    def test_model_view_bound_does_not_mutate_sources(self):
        captured=[]
        def transport(_,payload):
            captured.append(json.loads(payload["messages"][1]["content"]));return {"choices":[{"message":{"content":"{\"anchors\":[]}"}}]}
        source="α"*100000
        messages=[{"content":source} for _ in range(30)]
        client=ModelClient("gpt-4o-mini","dummy-not-a-real-key",transport=transport)
        client.extract(messages)
        self.assertLessEqual(len(captured[0]["snippets"]),24)
        self.assertLessEqual(sum(len(x["text"].encode()) for x in captured[0]["snippets"]),24000)
        self.assertEqual(messages[0]["content"],source)


if __name__=="__main__": unittest.main()
