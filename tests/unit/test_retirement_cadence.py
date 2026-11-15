"""Busy passes space retirement from completion; quiet passes can use the gap."""
from types import SimpleNamespace

from quarterdeck import post, post_retirement as retirement
from quarterdeck.service import Service


def service(runtime):
    runtime.mkdir()
    return SimpleNamespace(runtime=runtime, store=post.Store(runtime), attempts=post.Attempts(runtime),
        log=lambda *a, **kw: None, log_path=runtime/'log/service.jsonl')


def observation(s):
    s.post_retirement_observation = dict(seconds=30, read_seconds=29, phase='moves',
                                        candidates=1, retirable=1, blocked=False)


def test_busy_spacing_is_from_end_and_skips_do_not_move_the_deadline(tmp_path, monkeypatch):
    s = service(tmp_path/'runtime');clock=[0.0];calls=[]
    monkeypatch.setattr(retirement.time, 'monotonic', lambda: clock[0])
    def run(s, **kwargs):
        assert kwargs['complete_reads'] is True
        calls.append(clock[0]);clock[0]+=30;observation(s)
        return dict(attempts=1, journals=0)
    monkeypatch.setattr(retirement, 'run', run)
    retirement.due(s)
    for now in (31, 100, 300, 329):
        clock[0]=now;retirement.due(s)
        assert s.post_retirement_due == 330
    assert calls == [0]
    clock[0]=330;retirement.due(s)
    assert calls == [0,330]
    assert s.post_retirement_due == 660


def test_idle_pass_bypasses_busy_spacing(tmp_path, monkeypatch):
    s=service(tmp_path/'runtime');clock=[0.0];calls=[]
    monkeypatch.setattr(retirement.time, 'monotonic', lambda: clock[0])
    def run(s, **kwargs):
        calls.append(clock[0]);observation(s)
        return dict(attempts=1,journals=0)
    monkeypatch.setattr(retirement,'run',run)
    retirement.due(s)
    for now in (1,2,3):
        clock[0]=now;retirement.due(s,idle=True)
    assert calls == [0,1,2,3]
    clock[0]=4;retirement.due(s)
    assert calls == [0,1,2,3]


def test_spacing_itself_cannot_raise_no_progress_alert(tmp_path, monkeypatch):
    s=service(tmp_path/'runtime');clock=[0.0];wall=[1000.0];calls=[]
    monkeypatch.setattr(retirement.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(retirement.time,'time',lambda:wall[0])
    def run(s,**kwargs):
        calls.append(clock[0]);observation(s)
        return dict(attempts=0,journals=0,paused=True)
    monkeypatch.setattr(retirement,'run',run)
    retirement.due(s)
    clock[0]=299;wall[0]=1301
    retirement.due(s)
    assert calls == [0]
    assert retirement.reading(s.runtime) is None
    clock[0]=300;retirement.due(s)
    assert calls == [0,300]
    assert retirement.reading(s.runtime) == 'retirement stalled: moves'


def test_service_idle_exception_requires_no_actions_changes_or_accounting(tmp_path, monkeypatch):
    s=service(tmp_path/'runtime');seen=[]
    s.pass_had_work=False;s.retirement_inputs=retirement.activity_stamp(s.runtime)
    monkeypatch.setattr(retirement,'due',lambda service,**kwargs:seen.append(kwargs['idle']))
    Service.retire_post(s)
    assert seen[-1] is True
    Service.log(s,'post-retired',attempts=1,journals=0)
    Service.retire_post(s)
    assert seen[-1] is True
    Service.log(s,'delivery',seat='synthetic')
    Service.retire_post(s)
    assert seen[-1] is False
    s.pass_had_work=False
    s.store.new('answer','synthetic','ship','Synthetic silently filed arrival')
    Service.retire_post(s)
    assert seen[-1] is False
    s.retirement_inputs=retirement.activity_stamp(s.runtime)
    s.account_driver=SimpleNamespace(active=True)
    Service.retire_post(s)
    assert seen[-1] is False
    s.account_driver.active=False
    Service.retire_post(s)
    assert seen[-1] is True
    monkeypatch.setattr(retirement,'activity_stamp',lambda runtime:None)
    Service.retire_post(s)
    assert seen[-1] is False


def test_missing_scheduling_inputs_are_unknown():
    assert retirement.activity_stamp(None) is None
