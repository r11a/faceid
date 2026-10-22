"""Administrator-initiated connector setup; no recognition or door actions."""
import base64
import http.client
import ipaddress
import json
import logging
import re
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import HTTPException
from .access1 import atomic
from .access1_setup import prepare_certificate


def exchange_failure(stage, reason, status_code=502):
    # Only fixed internal codes reach this helper. Never log exception/response text.
    detail=f'Access1 pairing: stage={stage} reason={reason}'
    logging.getLogger(__name__).warning('%s',detail)
    return HTTPException(status_code,detail)


def decode(code, instance_id):
    try:
        if not isinstance(code,str) or len(code)>16000 or not code.startswith('A1P1.'):
            raise ValueError()
        value=json.loads(base64.b64decode(code[5:],altchars=b'-_',validate=True))
        if set(value)!={'version','session','instance_id','secret','origin','certificate'} or value['version']!=1:
            raise ValueError()
        if str(UUID(value['instance_id']))!=instance_id:raise ValueError()
        value['session']=str(UUID(value['session']))
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}',value['secret']):raise ValueError()
        target=urlsplit(value['origin'])
        if (target.scheme!='https' or target.port!=8445 or not target.hostname or
                not re.fullmatch(r'local-[a-z0-9-]+',target.hostname) or target.path or
                target.username or target.password or target.query or target.fragment):
            raise ValueError()
        if not isinstance(value['certificate'],str) or len(value['certificate'])>8192:
            raise ValueError()
        context=ssl.create_default_context(cadata=value['certificate'])
        context.verify_flags|=ssl.VERIFY_X509_PARTIAL_CHAIN
        return value
    except Exception:
        raise HTTPException(400,'Invalid setup code or different FaceID installation') from None


def exchange(value, method, credential, body=None):
    target=urlsplit(value['origin'])
    connection=None
    stage='dns'
    try:
        addresses={item[4][0] for item in socket.getaddrinfo(target.hostname,8445,family=socket.AF_INET,type=socket.SOCK_STREAM)}
        networks=tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
        if not addresses or any(not any(ipaddress.ip_address(a) in n for n in networks) for a in addresses):
            raise exchange_failure('dns','private_address_required')
        stage='tls_config'
        context=ssl.create_default_context(cadata=value['certificate'])
        context.verify_flags|=ssl.VERIFY_X509_PARTIAL_CHAIN
        connection=http.client.HTTPSConnection(target.hostname,8445,timeout=4,context=context)
        address=sorted(addresses)[0]
        connection._create_connection=lambda target,timeout,source_address:socket.create_connection((address,8445),timeout,source_address)
        stage='connect'
        connection.connect()
        stage='request'
        connection.request(method,'/faceid/v1/pairing/'+value['session'],
            body=json.dumps(body) if body is not None else None,
            headers={'Authorization':'Bearer '+credential,'Content-Type':'application/json'})
        stage='response'
        response=connection.getresponse()
        if response.status!=200:
            # HTTP status is numeric protocol metadata, not upstream response content.
            reason='http_'+str(response.status) if isinstance(response.status,int) and 100<=response.status<=599 else 'invalid_status'
            raise exchange_failure(stage,reason,410 if response.status==410 else 502)
        raw=response.read(16385)
        if len(raw)>16384:raise exchange_failure(stage,'response_too_large')
        stage='decode'
        result=json.loads(raw)
        if not isinstance(result,dict):raise exchange_failure(stage,'invalid_response')
        return result
    except HTTPException:raise
    except socket.gaierror:
        raise exchange_failure(stage,'dns_failed') from None
    except ssl.SSLCertVerificationError:
        raise exchange_failure('tls','certificate_verification_failed') from None
    except ssl.SSLError:
        raise exchange_failure('tls','tls_failed') from None
    except ConnectionRefusedError:
        raise exchange_failure(stage,'connection_refused') from None
    except TimeoutError:
        raise exchange_failure(stage,'timeout') from None
    except (json.JSONDecodeError,UnicodeDecodeError):
        raise exchange_failure(stage,'invalid_response') from None
    except OSError:
        raise exchange_failure(stage,'network_failed') from None
    except Exception:
        raise exchange_failure(stage,'operation_failed') from None
    finally:
        if connection:connection.close()


def submit(integration, code):
    value=decode(code,integration.config['instance_id'])
    with integration._lock:
        if integration.transport is not None:
            raise HTTPException(409,'Disconnect the current FaceID connector before replacing it')
        pending=getattr(integration,'_pairing',None)
        if pending and pending['session']==value['session'] and time.time()<pending['deadline']:
            if not secrets.compare_digest(pending['secret'],value['secret']):
                raise HTTPException(401,'Setup code does not match')
            exchange(value,'POST',value['secret'],pending['offer'])
            start_completion(integration,pending)
            return {'state':'review'}
        prepared=prepare_certificate(integration)
        directory_token=secrets.token_urlsafe(32)
        receipt=secrets.token_urlsafe(32)
        options={'enabled':True,'bind_address':prepared['bind_address'],
                 'tls_cert':prepared['tls_cert'],'tls_key':prepared['tls_key'],
                 'directory_token':directory_token}
        if integration.server:
            integration.server.should_exit=True
            # Uvicorn started remains true during shutdown; join its thread.
            if getattr(integration,'_directory_thread',None):
                integration._directory_thread.join(timeout=3)
                if integration._directory_thread.is_alive():
                    raise HTTPException(503,'Directory is stopping; retry shortly')
            integration.server=None
        integration.options=options
        integration.start_directory()
        deadline=time.monotonic()+3
        while not integration.server.started and time.monotonic()<deadline:time.sleep(.05)
        if not integration.server.started:raise HTTPException(503,'FaceID secure directory did not start')
        body={'instance_id':integration.config['instance_id'],
              'base_url':'https://'+prepared['bind_address']+':8601',
              'ca_pem':prepared['certificate'],'directory_token':directory_token,'receipt':receipt}
        pending={**value,'receipt':receipt,'options':options,'offer':body,'deadline':time.time()+300}
        # Keep retry material private; never return the code or tokens.
        integration._pairing=pending
        exchange(value,'POST',value['secret'],body)
        start_completion(integration,pending)
        return {'state':'review'}


def start_completion(integration,pending):
    if not pending.get('worker_started'):
        pending['worker_started']=True
        integration.state='pairing_review'
        threading.Thread(target=complete_in_background,args=(integration,pending['session']),
                         daemon=True,name='access1-pairing').start()


def complete_in_background(integration, session_id):
    """Finish after approval even when the user returns to the Access1 page."""
    for _ in range(75):
        time.sleep(4)
        try:
            result=finish(integration,expected_session=session_id)
            if result['state'] in ('connected','idle'):return
        except HTTPException as error:
            if error.status_code in (409,410):
                with integration._lock:
                    if getattr(integration,'_pairing',{}):
                        if integration._pairing['session']==session_id:
                            integration.state='pairing_expired'
                return
            # Bounded retries of setup status only; never resend recognition.
        except Exception:
            with integration._lock:
                pending=getattr(integration,'_pairing',None)
                if pending and pending['session']==session_id:
                    integration._pairing=None
                    integration.state='pairing_failed'
            return


def finish(integration, *, expected_session=None):
    with integration._lock:
        pending=getattr(integration,'_pairing',None)
        if expected_session and (not pending or pending['session']!=expected_session):return {'state':'idle'}
        transport=integration.transport
        if pending and time.time()>pending['deadline']:
            integration._pairing=None
            integration.state='pairing_expired'
            raise HTTPException(410,'Setup expired; start again in Access1')
    if not pending:
        if transport is None:return {'state':'idle'}
        try:transport.check_connection(integration.config['instance_id'])
        except Exception:
            raise HTTPException(502,'Connection could not be verified') from None
        with integration._lock:
            return {'state':'connected' if integration.transport is transport else 'idle'}
    # Access1 approval calls our directory while holding its transaction lock.
    # Never hold the directory's local lock while waiting for that transaction.
    result=exchange(pending,'GET',pending['receipt'])
    if result.get('state')!='approved':return {'state':result.get('state','review')}
    with integration._lock:
        if getattr(integration,'_pairing',None) is not pending:return {'state':'idle'}
        if result.get('instance_id')!=integration.config['instance_id']:
            raise HTTPException(409,'Access1 installation response mismatch')
        from .access1_transport import Transport
        folder=integration.path.parent/'access1-wizard-tls'
        ca=folder/'backend.pem'
        # Write only public trust material, never a private key from the peer.
        ca.write_text(pending['certificate'],encoding='ascii')
        options={**pending['options'],'access1_url':pending['origin'],
                 'access1_token':result.get('token',''),'access1_ca':str(ca),'paired':True}
        transport=Transport(options['access1_url'],options['access1_token'],options['access1_ca'],paired=True)
    transport.check_connection(integration.config['instance_id'])
    with integration._lock:
        if getattr(integration,'_pairing',None) is not pending:return {'state':'idle'}
        if time.time()>pending['deadline']:
            integration._pairing=None;integration.state='pairing_expired'
            raise HTTPException(410,'Setup expired; start again in Access1')
        atomic(integration.path.parent/'access1-connector.json',options)
        integration.options=options;integration.transport=transport
        integration.start_worker()
        integration.state='ready';integration._pairing=None
        return {'state':'connected'}


def disconnect(integration):
    with integration._lock:
        integration.transport=None;integration._pairing=None
        if integration.server:integration.server.should_exit=True
        integration.state='disabled'
        atomic(integration.path.parent/'access1-connector.json',{'enabled':False,'paired':True})
        return {'state':'disabled'}
