import tempfile
import unittest
from pathlib import Path
from h1bfinder import server as srv, db, ats


class JobListingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.previous = srv._main
        srv._main = db.connect(Path(cls.temp.name) / 'jobs.db')
        srv._main.executescript(srv.accounts.SCHEMA)

    @classmethod
    def tearDownClass(cls):
        srv.conn.close()
        srv._main.close()
        srv._main = cls.previous
        if hasattr(db._tl, 'conn'):
            del db._tl.conn
        cls.temp.cleanup()

    def setUp(self):
        for table in ('jobs', 'users', 'auth_sessions', 'auth_attempts', 'filings'):
            srv.conn.execute('DELETE FROM '+table)
        srv.conn.commit()
        self.c = srv.app.test_client()
        csrf = self.c.get('/api/auth/session').json['csrf']
        r = self.c.post('/api/auth/register', json={'username':'listing-user','password':'test-listing-password'},headers={'X-CSRF-Token':csrf})
        self.assertEqual(r.status_code,200)
        for i in range(28):
            srv.conn.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (str(i).zfill(2),'ACME','lever','acme',f'Engineer {i}', 'Austin, TX' if i%2 else 'London, UK',
                 i%2, f'https://jobs.lever.co/acme/{i}', '', 'Engineering', '2026-10-18', 100))
        # Repeated fiscal-year records must not duplicate job rows.
        srv.conn.executemany('INSERT INTO filings(norm,name,fy) VALUES(?,?,?)',
                            [('ACME','Acme Corporation',2025),('ACME','Acme Corporation',2026)])
        srv.conn.commit()

    def test_browse_without_resume_and_pagination(self):
        a=self.c.get('/api/jobs').json
        b=self.c.get('/api/jobs?page=2').json
        self.assertEqual(a['total'],28)
        self.assertEqual(len(a['results']),25)
        self.assertEqual(len(b['results']),3)
        self.assertFalse({x['uid'] for x in a['results']} & {x['uid'] for x in b['results']})
        self.assertEqual(a['results'][0]['employer'],'Acme Corporation')
        self.assertEqual(a['results'][0]['url'],'https://jobs.lever.co/acme/0')
        self.assertEqual(self.c.get('/api/jobs?page=999').json['page'],2)

    def test_filters_empty_results_and_input(self):
        self.assertEqual(self.c.get('/api/jobs?q=acme&us_only=1').json['total'],14)
        self.assertEqual(self.c.get('/api/jobs?location=london').json['total'],14)
        self.assertEqual(self.c.get('/api/jobs?q=Engineer%2027').json['total'],1)
        self.assertEqual(self.c.get('/api/jobs?q=%25').json['total'],0)
        self.assertEqual(self.c.get('/api/jobs?q=missing').json['results'],[])
        self.assertEqual(self.c.get('/api/jobs?page=nope').status_code,400)
        self.assertEqual(srv.app.test_client().get('/api/jobs').status_code,401)

    def test_unsafe_and_missing_urls_are_not_application_links(self):
        for value in ('javascript:alert(1)',None,'https://user:pass@example.com','https://['):
            srv.conn.execute('UPDATE jobs SET url=? WHERE uid=?',(value,'00'))
            srv.conn.commit()
            self.assertIsNone(self.c.get('/api/jobs').json['results'][0]['url'])


class SmartRecruitersExpiryTests(unittest.TestCase):
    def session(self, status):
        class Response:
            def __init__(self, code, data): self.status_code,self.data=code,data
            def json(self): return self.data
        class Session:
            def get(self,url,**kwargs):
                if '?' in url:
                    return Response(200,{'totalFound':2,'content':[
                        {'id':'expired','company':{'name':'Acme'}},
                        {'id':'active','company':{'name':'Acme'}}]})
                if url.endswith('/expired'): return Response(status,{})
                return Response(200,{'name':'Engineer','postingUrl':'https://jobs.smartrecruiters.com/acme/active',
                                     'location':{'city':'Austin','country':'US'}})
        return Session()

    def test_removed_posting_does_not_discard_other_jobs(self):
        for status in (404,410):
            found,name,jobs=ats.smartrecruiters(self.session(status),'acme')
            self.assertTrue(found)
            self.assertEqual(name,'Acme')
            self.assertEqual(len(jobs),1)
            self.assertEqual(jobs[0]['title'],'Engineer')

    def test_temporary_provider_error_still_preserves_cache(self):
        with self.assertRaises(ats.BoardRequestError):
            ats.smartrecruiters(self.session(503),'acme')

if __name__=='__main__': unittest.main()
