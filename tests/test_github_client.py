from pathlib import Path
import pytest, requests
from src.ingestion import github_client as gh

class Response:
    def __init__(self,status=200,data=None,headers=None,chunks=()):
        self.status_code=status; self._data=data or {}; self.headers=headers or {}; self.text='response'; self.chunks=chunks
    def json(self): return self._data
    def raise_for_status(self):
        if self.status_code>=400: raise requests.HTTPError(str(self.status_code))
    def iter_content(self,chunk_size): return iter(self.chunks)

class Session:
    def __init__(self,responses): self.responses=iter(responses); self.calls=[]
    def get(self,url,**kwargs):
        kwargs={**kwargs,'params':dict(kwargs['params'])} if 'params' in kwargs else kwargs
        self.calls.append((url,kwargs)); return next(self.responses)

class FailingSession:
    def __init__(self, failures, response): self.failures=failures; self.response=response; self.calls=0
    def get(self,url,**kwargs):
        self.calls += 1
        if self.calls <= self.failures: raise requests.ConnectionError('temporary')
        return self.response

def test_workflow_run_pagination(monkeypatch):
    page1=[{'id':i,'name':'CI'} for i in range(100)]; page2=[{'id':100,'name':'CI'}]
    session=Session([Response(data={'workflow_runs':page1}),Response(data={'workflow_runs':page2})])
    runs=gh.list_workflow_runs(session,'o/r',['CI'],max_runs=101)
    assert len(runs)==101 and [c[1]['params']['page'] for c in session.calls]==[1,2]

def test_403_non_rate_limit_is_not_retried():
    session=Session([Response(403,headers={'X-RateLimit-Remaining':'10'})])
    with pytest.raises(requests.HTTPError): gh._get_with_backoff(session,'url')
    assert len(session.calls)==1

def test_invalid_token_falls_back_once_for_public_requests():
    session=Session([Response(401),Response()])
    session.headers={'Authorization':'Bearer stale'}
    assert gh._get_with_backoff(session,'url').status_code==200
    assert 'Authorization' not in session.headers and len(session.calls)==2

def test_rate_limit_retries_and_stops_at_retry_cap(monkeypatch):
    waits=[]; monkeypatch.setattr(gh.time,'sleep',waits.append)
    session=Session([Response(429,headers={'Retry-After':'0'}) ,Response()])
    assert gh._get_with_backoff(session,'url').status_code==200 and len(waits)==1
    failed=Session([Response(429,headers={'Retry-After':'0'}) for _ in range(gh.MAX_RETRIES+1)])
    with pytest.raises(requests.HTTPError): gh._get_with_backoff(failed,'url')
    assert len(failed.calls)==gh.MAX_RETRIES+1

def test_network_and_server_errors_use_exponential_backoff(monkeypatch):
    waits=[]; monkeypatch.setattr(gh.time,'sleep',waits.append)
    network=FailingSession(2,Response())
    assert gh._get_with_backoff(network,'url').status_code==200
    assert waits==[1,2]
    waits.clear()
    server=Session([Response(503),Response(502),Response()])
    assert gh._get_with_backoff(server,'url').status_code==200
    assert waits==[1,2]

def test_expired_artifacts_are_filtered():
    data={'artifacts':[{'id':1,'name':'test-a','expired':False},{'id':2,'name':'test-b','expired':True}]}
    assert [r['id'] for r in gh.list_artifacts(Session([Response(data=data)]),'o/r',1,'test-')]==[1]

def test_download_streams_chunks_to_file(monkeypatch,tmp_path):
    monkeypatch.setattr(gh,'_get_with_backoff',lambda *a,**k:Response(chunks=[b'abc',b'',b'def']))
    path=gh.download_artifact_zip(Session([]),9,'o/r',str(tmp_path))
    assert Path(path).read_bytes()==b'abcdef'
