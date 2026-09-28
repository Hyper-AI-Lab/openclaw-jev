"""Contract, failure, isolation, and policy tests; no live provider calls."""
import asyncio
import copy
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock
import httpx
import pytest
from app.decisions import jev, memory
from app.decisions.jev import Evaluation, JevClient, MODEL, Policy, validate_response

QUESTIONS = {"q": {"type": "choice", "instructions": "Which?", "criteria": {"yes": "yes", "no": "no"}}}


def response(questions=QUESTIONS, choices=None):
    answers = {}
    for key, q in questions.items():
        labels = list(q["criteria"])
        default = {"support": "supported", "durability": "durable", "scope": "user"}.get(key.split("_")[0], labels[0])
        selected = (choices or {}).get(key, default)
        answers[key] = {"type": "choice", "choice": selected, "confidence": .99,
            "probabilities": {label: .99 if label == selected else .01 / (len(labels)-1) for label in labels}}
    return {"model": MODEL, "answers": answers, "usage": {"input_tokens": 100, "output_tokens": 0}}


@pytest.fixture
def clients(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-test-key")
    created = []
    def make(handler):
        c = JevClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        created.append(c)
        return c
    return make


async def evaluate(client, state=None, **kw):
    return await client.evaluate(state or {"text": "example"}, QUESTIONS,
        purpose=kw.pop("purpose", "test"), rubric=kw.pop("rubric", "v1"), policy=kw.pop("policy", Policy()), **kw)


@pytest.mark.parametrize("path,value", [
    (("model",), "jev-latest"), (("answers",), {}), (("usage",), {}),
    (("usage","input_tokens"), True), (("usage","input_tokens"), -1),
    (("answers","q","choice"), "other"), (("answers","q","type"), "score"),
    (("answers","q","confidence"), float('nan')), (("answers","q","confidence"), True),
    (("answers","q","confidence"), 1.01), (("answers","q","probabilities"), {"yes":.8,"no":.1}),
    (("answers","q","probabilities"), {"yes":True,"no":0}),
])
def test_contract_rejects_drift(path,value):
    body=response()
    at=body
    for key in path[:-1]: at=at[key]
    at[path[-1]]=value
    with pytest.raises(ValueError): validate_response(body,QUESTIONS)


def test_contract_discards_echoed_fields_and_accepts_rounding():
    body=response()
    body['private_echo']='secret narrative'
    body['answers']['q']['echo']='secret narrative'
    body['answers']['q']['probabilities']={'yes':.985,'no':.005}
    result=validate_response(body,QUESTIONS)
    assert 'secret narrative' not in json.dumps(result)
    body['answers']['extra']=copy.deepcopy(body['answers']['q'])
    with pytest.raises(ValueError): validate_response(body,QUESTIONS)


def test_many_option_rounding_is_accepted():
    labels=[f'o{i}' for i in range(10)]
    qs={'q':{'type':'choice','criteria':{label:None for label in labels}}}
    probabilities={label:(.59 if label=='o0' else .05) for label in labels}
    body={'model':MODEL,'usage':{'input_tokens':10,'output_tokens':0},'answers':{
        'q':{'type':'choice','choice':'o0','probabilities':probabilities,'confidence':.5}}}
    assert abs(sum(probabilities.values())-1)>0.021
    assert validate_response(body,qs)['answers']['q']['choice_is_max']


async def test_reported_choice_below_maximum_is_flagged_not_failed(clients):
    body=response()
    body['answers']['q']['probabilities']={'yes':.1,'no':.9}
    c=clients(lambda req:httpx.Response(200,json=body))
    ev=await evaluate(c)
    assert ev.status=='ok' and ev.result['answers']['q']['choice_is_max'] is False
    assert c.failures==0 and c.blocked_until==0
    await c.aclose()


def test_score_and_noul_contract():
    qs={'s':{'type':'score','criteria':['bad','good']},'b':{'type':'noul'}}
    body={'model':MODEL,'usage':{'input_tokens':10,'output_tokens':0},'answers':{
        's':{'type':'score','score':.75,'confidence':.8,'legend':{'0':'bad','1':'good'},'probabilities':{'0':.25,'1':.75}},
        'b':{'type':'noul','noul':.8}}}
    assert validate_response(body,qs)['answers']['b']=={'type':'noul','noul':.8}
    body['answers']['b']['noul']=float('inf')
    with pytest.raises(ValueError): validate_response(body,qs)


@pytest.mark.parametrize('field,value', [('timeout_sec',0),('timeout_sec',float('nan')),('cache_ttl_sec',301),
    ('requests_per_minute',True),('requests_per_minute',1.5),('promotion_mode','yes'),('intake_mode','on'),
    ('intake_min_confidence',-1),('intake_attach_min_confidence',1.5)])
def test_policy_bounds(field,value):
    with pytest.raises(ValueError): Policy(**{field:value})


def test_config_off_override(monkeypatch):
    import app.config
    monkeypatch.setattr(app.config,'load_settings',lambda:{'jev':{'promotion_mode':'enforce'}})
    monkeypatch.setenv('AURA_JEV_MODE','off')
    assert jev.get_policy().promotion_mode=='off' and jev.get_policy().intake_mode=='off'
    monkeypatch.setenv('AURA_JEV_MODE','invalid')
    assert jev.get_policy()==Policy()


async def test_cache_scope_credential_and_redaction(clients,monkeypatch,caplog):
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(200,json=response())
    c=clients(handler)
    caplog.set_level(logging.INFO,logger='rmp.jev')
    text='password=never-log-this'
    first=await evaluate(c,{'text':text})
    assert first.status=='ok'
    first.result['answers']['q']['choice']='mutated'
    cached=await evaluate(c,{'text':text})
    assert cached.cache_hit and cached.result['answers']['q']['choice']=='yes'
    await evaluate(c,{'text':text},purpose='another-scope')
    monkeypatch.setenv('TYPESAFE_API_KEY','rotated-test-key')
    await evaluate(c,{'text':text})
    assert len(calls)==3
    assert 'never-log-this' not in calls[0].content.decode()
    assert 'never-log-this' not in caplog.text and 'offline-test-key' not in caplog.text
    await c.aclose()


@pytest.mark.parametrize('key',['','space key',' key','é'])
async def test_missing_or_invalid_key_never_calls(clients,monkeypatch,key):
    monkeypatch.setenv('TYPESAFE_API_KEY',key)
    c=clients(lambda req:pytest.fail('outbound request'))
    assert (await evaluate(c)).reason=='missing_or_invalid_key'
    await c.aclose()


@pytest.mark.parametrize('status',[401,422,429,500,529])
async def test_http_failure_and_circuit(clients,status):
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(status,headers={'Retry-After':'60'},text='private failure')
    c=clients(handler)
    for _ in range(4): assert (await evaluate(c)).status=='unavailable'
    assert len(calls)==(1 if status in (429,529) else 3)
    await c.aclose()


async def test_size_limits_and_no_truncation(clients):
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(200,content=b'x'*(jev.MAX_RESPONSE_BYTES+1))
    c=clients(handler)
    assert (await evaluate(c,{'text':'日'*10000})).reason=='request_too_large'
    assert not calls
    assert (await evaluate(c)).status=='unavailable'
    assert len(calls)==1
    await c.aclose()


async def test_total_deadline_and_cancellation(clients):
    entered=asyncio.Event()
    async def handler(req):
        entered.set()
        await asyncio.Event().wait()
    c=clients(handler)
    assert (await evaluate(c,policy=Policy(timeout_sec=.05))).status=='unavailable'
    entered.clear()
    task=asyncio.create_task(evaluate(c))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    await c.aclose()


async def test_rate_and_cache_limits(clients):
    c=clients(lambda req:httpx.Response(200,json=response()))
    policy=Policy(requests_per_minute=1)
    assert (await evaluate(c,policy=policy)).status=='ok'
    assert (await evaluate(c,policy=policy)).cache_hit
    assert (await evaluate(c,{'text':'different'},policy=policy)).reason=='local_rate_limit'
    c.calls.clear()
    for i in range(jev.CACHE_SIZE+1):
        c.calls.clear()
        await evaluate(c,{'text':str(i)})
    assert len(c.cache)==jev.CACHE_SIZE
    await c.aclose()


@pytest.mark.parametrize('mode',['off','shadow','enforce'])
async def test_promotion_modes(clients,monkeypatch,mode):
    calls=[]
    def handler(req):
        calls.append(req)
        q=json.loads(req.content)['questions']
        return httpx.Response(200,json=response(q,{'durability_1':'transient','scope_1':'local'}))
    c=clients(handler)
    monkeypatch.setattr(memory,'get_client',lambda:c)
    monkeypatch.setattr(memory,'get_policy',lambda:Policy(promotion_mode=mode))
    report=await memory.review_promotions('source',[{'content':'preference'},{'content':'login'}],scope_key='p1')
    assert report['allowed_indices']==([0] if mode=='enforce' else [0,1])
    assert len(calls)==(0 if mode=='off' else 1)
    await c.aclose()


@pytest.mark.parametrize('fault',['outage','unknown','low_probability','model_drift','not_maximum'])
async def test_promotion_holds_uncertain_facts(clients,monkeypatch,fault):
    def handler(req):
        if fault=='outage': return httpx.Response(529)
        q=json.loads(req.content)['questions']
        body=response(q,{'scope_0':'unknown'} if fault=='unknown' else None)
        if fault=='low_probability': body['answers']['support_0']['probabilities']={'supported':.8,'unsupported':.1,'unknown':.1}
        if fault=='model_drift': body['model']='jev-latest'
        if fault=='not_maximum': body['answers']['scope_0']['probabilities']={'user':.3,'local':.6,'unknown':.1}
        return httpx.Response(200,json=body)
    c=clients(handler)
    monkeypatch.setattr(memory,'get_client',lambda:c)
    monkeypatch.setattr(memory,'get_policy',lambda:Policy(promotion_mode='enforce'))
    report=await memory.review_promotions('source',[{'content':'fact'}],scope_key='p')
    assert report['allowed_indices']==[] and report['held_indices']==[0]
    await c.aclose()


async def test_actual_promotion_callsite_holds_before_write(monkeypatch):
    from app.memory.promotion import promote_completion_memory
    from app.memory.router import MemoryRouter
    writer=AsyncMock()
    monkeypatch.setattr(MemoryRouter,'write',writer)
    monkeypatch.setattr(memory,'review_promotions',AsyncMock(return_value={'mode':'enforce','allowed_indices':[],'held_indices':[0]}))
    stats=await promote_completion_memory(process_run_id='p',process_type='',task_id='t',
        episodic_content='The task inspected https://example.org/pricing for available options.')
    assert stats['jev_held']==1 and stats['promoted_semantic']==0 and stats['promoted_pinned']==0
    writer.assert_not_awaited()


async def test_eval_metrics_include_false_holds(monkeypatch):
    from ops import jev_eval
    cases=jev_eval.load_cases(Path('tests/fixtures/jev_memory_eval.jsonl'),20)
    fake=type('Fake',(),{'evaluate':AsyncMock(return_value=Evaluation('unavailable','test'))})()
    monkeypatch.setattr(jev_eval,'get_client',lambda:fake)
    monkeypatch.setattr(jev_eval,'close_jev_client',AsyncMock())
    monkeypatch.setattr(jev_eval,'MIN_REQUEST_SPACING_SEC',0)
    metrics=await jev_eval.run(cases,Policy(cache_ttl_sec=0))
    assert metrics['unavailable']==7
    assert metrics['promotion']['false_holds']==2
    assert metrics['promotion']['precision_on_accepted'] is None


async def test_concurrency_queue_is_inside_total_deadline(clients):
    active=0
    both=asyncio.Event()
    async def handler(req):
        nonlocal active
        active+=1
        if active==2: both.set()
        try:
            await asyncio.Event().wait()
        finally:
            active-=1
    c=clients(handler)
    tasks=[asyncio.create_task(evaluate(c)) for _ in range(2)]
    await both.wait()
    queued=await evaluate(c,policy=Policy(timeout_sec=.05))
    assert queued.status=='unavailable' and active==2
    for t in tasks: t.cancel()
    await asyncio.gather(*tasks,return_exceptions=True)
    await c.aclose()


async def test_body_read_is_inside_deadline(clients):
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{'
            await asyncio.Event().wait()
    c=clients(lambda req:httpx.Response(200,stream=SlowBody()))
    assert (await evaluate(c,policy=Policy(timeout_sec=.05))).status=='unavailable'
    await c.aclose()


async def test_cache_rubric_and_disable(clients):
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(200,json=response())
    c=clients(handler)
    await evaluate(c)
    await evaluate(c,rubric='v2')
    await evaluate(c,policy=Policy(cache_ttl_sec=0))
    assert len(calls)==3
    await c.aclose()


async def test_empty_and_oversized_consumers_do_not_call(monkeypatch):
    monkeypatch.setattr(memory,'get_policy',lambda:Policy(promotion_mode='enforce'))
    monkeypatch.setattr(memory,'get_client',lambda:pytest.fail('unexpected provider call'))
    assert (await memory.review_promotions('episode',[],scope_key='p'))['allowed_indices']==[]
    report=await memory.review_promotions('episode',[{'content':'x'}]*9,scope_key='p')
    assert report['allowed_indices']==[] and report['reason']=='candidate_limit'


async def test_pooled_client_cleanup():
    first=jev.get_client()
    assert jev.get_client() is first
    await jev.close_jev_client()
    assert first.client.is_closed
    second=jev.get_client()
    assert second is not first
    await jev.close_jev_client()
