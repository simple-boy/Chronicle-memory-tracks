import unittest

from forgetrace.parsing import query_features,parse_message
from forgetrace.retrieval import rank_candidates,excerpt_ranges


class RetrievalPolicyTests(unittest.TestCase):
    def test_no_reliability_preserves_applicability(self):
        query=query_features('Fix src/api.py under SDK==2.0.')
        rows=[{'id':'old','base_score':100,'sequence':1,'features':parse_message('src/api.py SDK==1.0. Tests passed.')},
              {'id':'current','base_score':1,'sequence':2,'features':parse_message('src/api.py SDK==2.0. Tests failed.')}]
        ranked=rank_candidates(rows,query,'proposed','no_reliability')
        self.assertEqual(ranked[0]['id'],'current')
        self.assertEqual(ranked[0]['rank_key'][2],0)
        self.assertLess(ranked[1]['rank_key'][0],0)

    def test_no_applicability_retains_reported_reliability(self):
        query=query_features('Fix src/api.py under SDK==2.0.')
        rows=[{'id':'old','base_score':100,'sequence':1,'features':parse_message('src/api.py SDK==1.0. Tests passed.')},
              {'id':'current','base_score':1,'sequence':2,'features':parse_message('src/api.py SDK==2.0. Tests failed.')}]
        ranked=rank_candidates(rows,query,'proposed','no_applicability')
        self.assertEqual(ranked[0]['id'],'old')
        self.assertEqual(ranked[0]['rank_key'][0],0)
        self.assertEqual(ranked[0]['rank_key'][2],1)

    def test_long_source_never_drops_observed_negative_tail(self):
        text='Action: remove validation.\n'+'padding '*3000+'\nTests failed.'
        features=parse_message(text)
        selected=excerpt_ranges(text,0,100,features)
        self.assertTrue(selected)
        self.assertIn('Tests failed.',''.join(text[a:b] for a,b in selected))


if __name__=='__main__': unittest.main()
