"""Run with: python -m unittest discover -s tests -p test_accounts.py"""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory()
os.environ['H1B_DB_PATH'] = str(Path(TEMP.name) / 'test.db')
from h1bfinder import server as srv

TEXT = 'Python engineer with 6 years of experience building SQL and AWS applications. '

def tearDownModule():
    srv.conn.close()
    srv._main.close()
    if hasattr(srv.db._tl, 'conn'):
        del srv.db._tl.conn
    TEMP.cleanup()

class AccountsTest(unittest.TestCase):
    def setUp(self):
        for table in ('resumes','users','auth_sessions','auth_attempts'):
            srv.conn.execute('DELETE FROM '+table)
        srv.conn.commit()
        self.a = srv.app.test_client()
        self.b = srv.app.test_client()

    def csrf(self,c):
        return {'X-CSRF-Token':c.get('/api/auth/session').json['csrf']}

    def post(self,c,url,**kwargs):
        return c.post(url,headers=self.csrf(c),**kwargs)

    def register(self,c,name):
        r=self.post(c,'/api/auth/register',json={'username':name,'password':'long-test-password'})
        self.assertEqual(r.status_code,200,r.json)

    def upload(self,c,text=TEXT):
        r=self.post(c,'/api/resume',json={'text':text})
        self.assertEqual(r.status_code,200,r.json)
        return r.json['id']

    def test_auth_csrf_logout_and_password(self):
        self.assertEqual(self.a.get('/').status_code,302)
        self.assertEqual(self.a.get('/api/resumes').status_code,401)
        self.assertEqual(self.a.post('/api/auth/register',json={}).status_code,403)
        self.register(self.a,'alice')
        self.assertEqual(self.a.get('/').status_code,200)
        hashed=srv.conn.execute('SELECT password_hash FROM users').fetchone()[0]
        self.assertNotEqual(hashed,'long-test-password')
        self.assertEqual(self.post(self.b,'/api/auth/login',json={'username':'alice','password':'wrong-password'}).status_code,401)
        self.assertEqual(self.a.post('/api/resume',json={'text':TEXT}).status_code,403)
        cookie=self.a.get_cookie('session').value
        self.post(self.a,'/api/auth/logout',json={})
        self.a.set_cookie('session',cookie)
        self.assertEqual(self.a.get('/api/resumes').status_code,401)
        self.assertEqual(self.post(self.a,'/api/auth/login',json={'username':'ALICE','password':'long-test-password'}).status_code,200)

    def test_private_library_and_active_matching(self):
        self.register(self.a,'alice');one=self.upload(self.a)
        two=self.upload(self.a,TEXT+' Java Kubernetes')
        self.register(self.b,'bob')
        self.assertEqual(self.b.get('/api/resumes').json['resumes'],[])
        self.assertIsNone(self.b.get('/api/status').json['resume'])
        for suffix in ('','/download'):
            self.assertEqual(self.b.get(f'/api/resumes/{one}'+suffix).status_code,404)
        for suffix in ('/activate','/delete'):
            self.assertEqual(self.post(self.b,f'/api/resumes/{one}'+suffix,json={}).status_code,404)
        self.assertEqual(self.a.get('/api/status').json['resume']['id'],two)
        self.post(self.a,f'/api/resumes/{one}/activate',json={})
        with patch.object(srv.matcher,'rank',return_value=[]) as rank:
            self.assertEqual(self.a.get('/api/recommendations').status_code,200)
            self.assertEqual(rank.call_args.args[1],TEXT)
        self.assertEqual(self.a.get(f'/api/resumes/{one}').json['text'],TEXT)
        self.post(self.a,f'/api/resumes/{one}/delete',json={})
        self.assertIsNone(self.a.get('/api/status').json['resume'])
        self.assertEqual(self.a.get('/api/recommendations').status_code,400)
        self.assertEqual(len(self.a.get('/api/resumes').json['resumes']),1)

    def test_upload_download_validation_and_legacy(self):
        srv.db.set_meta(srv.conn,'resume_text','LEGACY '+TEXT)
        self.register(self.a,'alice')
        self.assertIsNone(self.a.get('/api/status').json['resume'])
        self.assertEqual(self.post(self.a,'/api/resume',json={'text':'short'}).status_code,400)
        self.assertEqual(self.post(self.a,'/api/resume',json={'text':123}).status_code,400)
        r=self.post(self.a,'/api/resume',data={'file':(io.BytesIO(TEXT.encode()),'resume.txt')})
        self.assertEqual(r.status_code,200,r.json)
        download=self.a.get(f"/api/resumes/{r.json['id']}/download")
        self.assertEqual(download.data,TEXT.encode())
        self.assertIn('attachment',download.headers['Content-Disposition'])
        self.assertEqual(download.headers['Cache-Control'],'no-store')
        self.assertEqual(self.post(self.a,'/api/resume',data={'file':(io.BytesIO(b'bad'),'resume.pdf')}).status_code,400)
        self.assertEqual(len(self.a.get('/api/resumes').json['resumes']),1)

    def test_duplicate_account_and_rate_limit(self):
        self.register(self.a,'alice')
        self.assertEqual(self.post(self.b,'/api/auth/register',json={'username':'ALICE','password':'long-test-password'}).status_code,409)
        import time
        srv.conn.executemany('INSERT INTO auth_attempts VALUES(?,?)',[('127.0.0.1',time.time())]*30)
        srv.conn.commit()
        self.assertEqual(self.post(self.b,'/api/auth/login',json={}).status_code,429)

if __name__=='__main__': unittest.main()
