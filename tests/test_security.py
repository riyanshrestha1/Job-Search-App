import io
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from h1bfinder import server as srv, db, dataset_download as downloads
from h1bfinder.admin import set_admin


class AdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.previous=srv._main
        srv._main=db.connect(Path(cls.temp.name)/'admin.db')
        srv._main.executescript(srv.accounts.SCHEMA)

    @classmethod
    def tearDownClass(cls):
        srv.conn.close();srv._main.close();srv._main=cls.previous
        if hasattr(db._tl,'conn'): del db._tl.conn
        cls.temp.cleanup()

    def setUp(self):
        for table in ('administrators','users','auth_sessions','auth_attempts'):
            srv.conn.execute('DELETE FROM '+table)
        srv.conn.commit()
        self.c=srv.app.test_client()
        token=self.c.get('/api/auth/session').json['csrf']
        r=self.c.post('/api/auth/register',json={'username':'owner','password':'test-owner-password','is_admin':True},headers={'X-CSRF-Token':token})
        self.assertEqual(r.status_code,200)
        self.headers={'X-CSRF-Token':self.c.get('/api/auth/session').json['csrf']}

    def test_registration_cannot_claim_admin_and_dataset_writes_are_denied(self):
        self.assertFalse(self.c.get('/api/auth/session').json['user']['is_admin'])
        self.assertEqual(self.c.get('/api/datasets').status_code,200)
        with patch.object(srv.uscis,'ingest_url') as ingest, patch.object(srv.uscis,'remove_dataset') as remove:
            for action in ('discover','ingest','upload','remove'):
                r=self.c.post('/api/datasets/'+action,json={},headers=self.headers)
                self.assertEqual(r.status_code,403,action)
            ingest.assert_not_called();remove.assert_not_called()
        self.assertEqual(srv.app.test_client().post('/api/datasets/remove',json={}).status_code,401)

    def test_grant_revoke_and_csrf_for_admin(self):
        set_admin(srv.conn,'OWNER',True)
        self.assertTrue(self.c.get('/api/auth/session').json['user']['is_admin'])
        with patch.object(srv.uscis,'remove_dataset') as remove, patch.object(srv.index,'refresh'):
            self.assertEqual(self.c.post('/api/datasets/remove',json={'source':'example'}).status_code,403)
            remove.assert_not_called()
            self.assertEqual(self.c.post('/api/datasets/remove',json={'source':'example'},headers=self.headers).status_code,200)
            remove.assert_called_once()
            set_admin(srv.conn,'owner',False)
            self.assertEqual(self.c.post('/api/datasets/remove',json={},headers=self.headers).status_code,403)
        with self.assertRaises(ValueError): set_admin(srv.conn,'missing',True)

    def test_admin_import_upload_and_discovery(self):
        set_admin(srv.conn,'owner',True)
        with patch.object(srv.index,'refresh'), patch.object(srv.uscis,'ingest_url',return_value={'rows':1}) as ingest:
            self.assertEqual(self.c.post('/api/datasets/ingest',json={'url':'https://www.uscis.gov/sites/default/files/data.csv'},headers=self.headers).status_code,200)
            ingest.assert_called_once()
        with patch.object(srv.uscis,'discover_links',return_value=[]):
            self.assertEqual(self.c.post('/api/datasets/discover',json={},headers=self.headers).status_code,200)
        with patch.object(srv.index,'refresh'), patch.object(srv.uscis,'ingest_bytes',return_value={'rows':1}):
            self.assertEqual(self.c.post('/api/datasets/upload',data={'file':(io.BytesIO(b'test'),'data.csv')},headers=self.headers).status_code,200)


class DownloadTests(unittest.TestCase):
    url='https://www.uscis.gov/sites/default/files/document/data/example.csv'

    def test_disallowed_urls_never_reach_network(self):
        urls=['http://www.uscis.gov/sites/default/files/a.csv','https://127.0.0.1/a.csv',
              'http://169.254.169.254/latest/meta-data','file:///etc/passwd',
              'https://www.uscis.gov.evil.test/sites/default/files/a.csv',
              'https://evil.test@www.uscis.gov/sites/default/files/a.csv',
              'https://www.uscis.gov:8443/sites/default/files/a.csv',
              'https://www.uscis.gov/sites/default/files/%2e%2e/a.csv',
              'https://www.uscis.gov/redirect?url=http://localhost',
              'https://www.uscis.gov\\@localhost/sites/default/files/a.csv',
              'https://www.uscis.gov/sites/default/files/a.csv\n',None]
        with patch.object(downloads.requests,'Session') as request,patch.object(downloads.socket,'getaddrinfo') as dns:
            for url in urls:
                with self.subTest(url=url),self.assertRaises(ValueError): downloads.download(url)
            request.assert_not_called();dns.assert_not_called()

    def test_private_or_mixed_dns_is_rejected(self):
        for ip in ('127.0.0.1','10.0.0.1','169.254.169.254','::1','fc00::1'):
            rows=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443))]
            with patch.object(downloads.socket,'getaddrinfo',return_value=rows),patch.object(downloads.requests,'Session') as request:
                with self.assertRaises(ValueError): downloads.download(self.url)
                request.assert_not_called()

    def response(self,status=200,chunks=(b'a,b\n1,2',),headers=None):
        response=MagicMock();response.status_code=status;response.headers=headers or {}
        response.iter_content.return_value=iter(chunks)
        response.__enter__.return_value=response
        return response

    def run_download(self,response):
        session=MagicMock();session.__enter__.return_value=session;session.request.return_value=response
        dns=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))]
        with patch.object(downloads.socket,'getaddrinfo',return_value=dns),patch.object(downloads.requests,'Session',return_value=session):
            result=downloads.download(self.url)
        self.assertFalse(session.trust_env)
        self.assertFalse(session.request.call_args.kwargs['allow_redirects'])
        return result

    def test_valid_download_and_redirect_block(self):
        self.assertEqual(self.run_download(self.response()),b'a,b\n1,2')
        for status in (301,302,307,308):
            with self.assertRaises(ValueError): self.run_download(self.response(status,headers={'Location':'http://127.0.0.1'}))

    def test_download_size_limits_with_and_without_content_length(self):
        with self.assertRaises(ValueError): self.run_download(self.response(headers={'Content-Length':str(downloads.MAX_BYTES+1)}))
        with patch.object(downloads,'MAX_BYTES',4):
            with self.assertRaises(ValueError): self.run_download(self.response(chunks=(b'123',b'45')))

if __name__=='__main__': unittest.main()
