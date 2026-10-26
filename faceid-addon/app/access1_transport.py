"""Bounded HTTPS transport for identity events, never door commands."""
import http.client
import json
import ssl
from urllib.parse import urlsplit
from uuid import UUID


class Transport:
    def __init__(self,url,token,ca,*,paired=False):
        target=urlsplit(url)
        if (target.scheme!='https' or not target.hostname or target.username or target.password or
                target.path not in ('','/') or target.query or target.fragment):
            raise ValueError('Access1 must be an HTTPS origin')
        if not token.startswith('acs_face_') or not 32<=len(token)<=200:raise ValueError('Service token required')
        self.host,self.port,self.token=target.hostname,target.port or 443,token
        self.context=ssl.create_default_context(cafile=ca or None)
        if paired:self.context.verify_flags|=ssl.VERIFY_X509_PARTIAL_CHAIN

    def call(self,method,event_id,payload=None):
        event_id=str(UUID(event_id))
        path='/faceid/v1/events'+('/'+event_id if method=='GET' else '')
        result=self._request(method,path,payload)
        if (result.get('event_id')!=event_id or result.get('source')!='faceid' or
                result.get('status') not in ('pending','accepted','denied','cancelled','expired','unconfirmed','dispatched','completed','failed')):
            raise ValueError('Unexpected Access1 result')
        return {key:result.get(key) for key in ('event_id','request_id','status','reason','execution_confirmed','door_open_confirmed')}

    def report_security(self,instance_id,secure):
        result=self._request('POST','/faceid/v1/secure-status',
            {'instance_id':str(UUID(instance_id)),'secure':secure,'schema_version':1})
        if (result.get('instance_id')!=str(UUID(instance_id)) or result.get('status')!='status_received'):
            raise ValueError('Unexpected Access1 status acknowledgement')
        return True

    def check_connection(self,instance_id):
        result=self._request('GET','/faceid/v1/connection')
        if (result.get('instance_id')!=str(UUID(instance_id))
                or result.get('status')!='connection_verified'
                or type(result.get('requests_enabled')) is not bool):
            raise ValueError('Unexpected Access1 connection result')
        return {key:result[key] for key in ('instance_id','status','requests_enabled')}

    def _request(self,method,path,payload=None):
        connection=http.client.HTTPSConnection(self.host,self.port,timeout=2,context=self.context)
        try:
            connection.request(method,path,body=json.dumps(payload) if payload else None,
                headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
            response=connection.getresponse();raw=response.read(16385)
            if response.status!=200 or len(raw)>16384:raise ValueError('Access1 response unavailable')
            result=json.loads(raw)
            if not isinstance(result,dict):raise ValueError('Unexpected Access1 response')
            return result
        finally:connection.close()
