import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import release
import verify_submission


class ReleaseTests(unittest.TestCase):
    def test_private_env_and_runtime_memory_excluded(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name in ['.env','.env.example','README.md','src/x.py','data/private.txt','artifacts/private/key.json','src/__pycache__/x.pyc','results/report.json']:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('safe',encoding='utf-8')
            selected={p.relative_to(root).as_posix() for p in release.release_files(root)}
            self.assertEqual(selected,{'.env.example','README.md','src/x.py','results/report.json'})

    def test_source_hash_detects_runtime_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);package=root/'src/forgetrace';package.mkdir(parents=True)
            path=package/'a.py';path.write_text('before')
            first=release.source_hash(root);path.write_text('after')
            self.assertNotEqual(first,release.source_hash(root))

    def test_zip_manifest_rejects_source_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);package=root/'src/forgetrace';package.mkdir(parents=True)
            (package/'a.py').write_text('before')
            (root/'release-manifest.json').write_text(json.dumps({'commit':'a'*40,'source_sha256':release.source_hash(root)}))
            self.assertEqual(release.project_revision(root),('a'*40,False))
            (package/'a.py').write_text('after')
            with self.assertRaises(ValueError): release.project_revision(root)

    def test_readiness_rejects_offline_even_with_operator_flags(self):
        args=type('Args',(),dict(base_url='http://localhost',commit='a'*40,source_sha256='b'*64,division='open-source',
            write=False,eligibility_evidence='contract snapshot',confirm_disk=True,confirm_version=True,confirm_no_private_data=True))()
        def request(base,path,**kwargs):
            if path=='/health': return 200,{'service':'forgetrace','track':'coding','commit':'a'*40,'source_sha256':'b'*64,'model_mode':'offline','diagnostic_only':True},0
            if path=='/metrics': return 200,{'storage':{'disk_free_bytes':2*1024**3}},0
            return 401,{},0
        with mock.patch.object(verify_submission,'request',side_effect=request): result=verify_submission.verify(args,'test')
        self.assertFalse(result['ready_for_submission'])
        self.assertIn('offline_model_not_cleared_for_open_source_submission',result['blockers'])


if __name__=='__main__': unittest.main()
