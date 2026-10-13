"""Parallel background sending, a human voice for Luna, and the Geotab typing and discipline fixes.

Browsers, agents, and employers are local fakes; nothing is sent anywhere.
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest
from pydantic import ValidationError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JOBHUB_REDUCED_MOTION", "1")

from jobhub import config, db, prepare, scheduler  # noqa: E402
from jobhub.answers import Q, _field_text, factual_essay, natural_list, relevant_skills  # noqa: E402
from jobhub.apply import engine as engine_module  # noqa: E402
from jobhub.models import Education, Job, Profile  # noqa: E402

SKILLS = ["Python", "R", "SQL", "C++", "C#", "TypeScript", "JavaScript", "React", "Node.js", "Express", "PyTorch",
          "TensorFlow", "Scikit-learn", "Pandas", "Docker", "Firebase", "Vercel", "LLM Integration", "Vulkan"]


def posting(company="Example", number=1, description="We build web apps in React and TypeScript on AWS."):
    return Job(source="greenhouse", ats="greenhouse", company=company, title="Software Developer",
               url=f"https://job-boards.greenhouse.io/{company.lower()}/jobs/{number}", description=description)


# ------------------------------------------------------------------ Geotab: typing and the discipline
def test_long_answers_get_time_to_be_typed(monkeypatch):
    calls = []

    class Field:
        def press_sequentially(self, text, delay, timeout):
            calls.append((len(text), delay, timeout))

    form = engine_module.FormEngine.__new__(engine_module.FormEngine)
    form._rng = __import__("random").Random(1)
    monkeypatch.setenv("JOBHUB_HUMAN_PACING", "1")
    form._type(Field(), "x" * 288)
    length, delay, timeout = calls[0]
    assert delay >= 18 and timeout >= length * delay + 5000 > 7000    # a 288-character answer at human speed
    monkeypatch.setenv("JOBHUB_HUMAN_PACING", "0")
    form._type(Field(), "short")
    assert calls[1][1] == 0 and calls[1][2] == 7000


def test_degree_names_keep_their_discipline():
    def field(degree):
        return _field_text(Education(degree=degree))
    assert field("Master of Science in Quantitative Economics") == "Quantitative Economics"
    assert field("Bachelor of Science in Computer Science") == "Computer Science"
    assert field("Masters of Business Administration in Finance") == "Finance"
    assert field("MA Economics") == "Economics" and field("B.S. Computer Science") == "Computer Science"
    assert field("Mathematics") == "Mathematics" and field("MBA") == "MBA"


# ------------------------------------------------------------------ choosing a few relevant skills
def test_a_skills_question_names_a_few_relevant_skills_not_all_of_them():
    profile = Profile(skills=SKILLS)
    job = posting()
    frameworks = relevant_skills(profile, job, "What frameworks/libraries are you most proficient in?")
    assert frameworks[:1] == ["React"] and len(frameworks) <= 5
    assert not {"Python", "R", "SQL", "C++"} & set(frameworks)         # languages are not frameworks
    cloud = relevant_skills(profile, job, "Do you have experience working with cloud computing? (GCP/Azure/AWS)")
    assert set(cloud) <= {"Docker", "Firebase", "Vercel"}
    ai = relevant_skills(profile, job, "What are some AI specific technologies you are comfortable with?")
    assert {"PyTorch", "TensorFlow", "LLM Integration"} <= set(ai)
    answer = factual_essay(Q("What technical skills do you use most?", "text"), profile, job)
    assert answer.value.count(",") <= 4 and " and " in answer.value
    assert natural_list(["Python", "SQL", "C++"]) == "Python, SQL, and C++" and natural_list(["A", "B"]) == "A and B"


def test_luna_is_asked_for_a_human_voice_and_gets_the_relevant_skills(store, monkeypatch):
    from jobhub import luna

    prompts = []

    def fake(prompt, settings, timeout=0):
        prompts.append(prompt)
        return json.dumps({"answers": [{"id": "q1", "answer": (
            "I use Python, R, SQL, C++, C#, TypeScript, JavaScript, React, and Node.js every day.")},
            {"id": "q2", "answer": "Mostly React with TypeScript; I built a trading dashboard with it."}]})

    monkeypatch.setattr(luna, "complete", fake)
    profile = Profile(first_name="Ada", skills=SKILLS)
    questions = [prepare._question(1, "What tools do you use?", "textarea", [], True),
                 prepare._question(2, "What frameworks/libraries are you most proficient in?", "textarea", [], True)]
    out = luna.autofill_answers(posting(), profile, db.get_settings(), questions)
    prompt = prompts[0]
    assert "VOICE" in prompt and "Never list every skill" in prompt and "I am passionate about" in prompt
    asked = json.loads(prompt.split("QUESTIONS")[-1])
    assert asked[1]["relevant_skills"][0] == "React" and len(asked[1]["relevant_skills"]) <= 4
    assert "trim to the few this role uses" in out["q1"]["note"]       # a skill dump is flagged for review
    assert "trim" not in out["q2"]["note"]


# ------------------------------------------------------------------ parallel reservations
def test_parallel_reservations_are_bounded_and_one_per_posting(store):
    jobs = [posting(f"Company{i}", i) for i in range(4)]
    db.upsert_jobs(jobs)
    held = [db.reserve_application(job.id, limit=3) for job in jobs[:3]]
    assert len(set(held)) == 3
    with pytest.raises(ValueError, match="waits its turn"):
        db.reserve_application(jobs[3].id, limit=3)
    with pytest.raises(ValueError, match="already queued or in progress"):
        db.reserve_application(jobs[0].id, limit=8)                     # never twice for one posting
    with pytest.raises(ValueError, match="^An application is already queued"):
        db.reserve_application(jobs[3].id)                              # the default stays one at a time


def test_a_full_queue_leaves_the_draft_approved_for_a_retry(store, make_pdf, monkeypatch):
    resume = make_pdf(store / "resumes" / "main.pdf", ["Ada Example"])
    db.save_profile(Profile(first_name="Ada", email="ada@example.test", resume_path=str(resume)))
    busy, mine = posting("Busy", 1), posting("Mine", 2)
    db.upsert_jobs([busy, mine])
    db.reserve_application(busy.id)
    draft_id = db.upsert_draft(mine.id, status="ready")
    prepare.approve_and_queue(draft_id)
    result = prepare.send_draft(draft_id, parallel=1)
    assert result["status"] == "busy"
    assert db.get_draft(draft_id)["status"] == "approved"


def test_each_simultaneous_send_has_its_own_browser_profile(store):
    with engine_module.browser_profile_slot() as first, engine_module.browser_profile_slot() as second:
        assert first == config.BROWSER_PROFILE and second != first and second.is_dir()
        assert second.is_relative_to(config.BROWSER_PROFILE)
    with engine_module.browser_profile_slot() as again:
        assert again == config.BROWSER_PROFILE


def test_sending_runs_without_a_window_unless_asked(monkeypatch):
    launched = []

    class Context:
        def add_init_script(self, script):
            launched[-1].setdefault("scripts", []).append(script)

    class Chromium:
        def launch_persistent_context(self, profile, **kwargs):
            launched.append(dict(kwargs, profile=profile))
            return Context()

        def launch(self, **kwargs):
            raise AssertionError("the user agent is cached for this test")

    monkeypatch.setenv("JOBHUB_HUMAN_PACING", "1")
    monkeypatch.setattr(engine_module, "_HEADLESS_UA", "Mozilla/5.0 Chrome/153.0.0.0 Safari/537.36")
    playwright = type("P", (), {"chromium": Chromium()})()
    engine_module.launch_context(playwright, None, config.BROWSER_PROFILE)
    background = launched[-1]
    assert background["headless"] is True and background["channel"] == "chromium"
    assert "HeadlessChrome" not in background["user_agent"]
    assert engine_module.HEADLESS_WINDOW_SCRIPT in background["scripts"]
    assert not any("window-position" in arg for arg in background["args"])
    engine_module.launch_context(playwright, None, config.BROWSER_PROFILE, visible=True)
    assert launched[-1]["headless"] is False
    monkeypatch.setattr(config, "HEADLESS", True)
    assert engine_module.show_browser({"show_browser": True}) and not engine_module.show_browser({})


# ------------------------------------------------------------------ the controller's send pool
@pytest.fixture
def controller(store, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from jobhub.desktop.controller import DesktopController
    from jobhub.desktop.ui import JobHubWindow

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(scheduler, "rescore", lambda: None)
    db.set_settings({"setup_complete": True, "parallel_sends": 3})
    window = JobHubWindow()
    instance = DesktopController(window, scheduler_enabled=False, show_setup_on_start=False)
    instance.app = app
    yield instance
    instance.stop()
    window.close()
    window.deleteLater()
    scheduler.STATE.stop = False


def test_send_pool_runs_several_at_once_but_one_per_employer(controller, monkeypatch):
    companies = ["Alpha", "Beta", "Gamma", "Delta", "Alpha"]
    jobs = [posting(company, index) for index, company in enumerate(companies)]
    db.upsert_jobs(jobs)
    drafts = [db.upsert_draft(job.id, status="approved") for job in jobs]
    lock, active, peak, seen_together = threading.Lock(), set(), [0], []
    release = threading.Event()

    def fake_send(draft_id, parallel=1, assist=False):
        assert parallel == 3
        company = db.get_draft(draft_id)["company"]
        with lock:
            active.add(draft_id)
            peak[0] = max(peak[0], len(active))
            seen_together.append(sorted(db.get_draft(d)["company"] for d in active))
        release.wait(5)
        time.sleep(0.05)
        with lock:
            active.discard(draft_id)
        return {"status": "applied", "draft_id": draft_id, "company": company}

    monkeypatch.setattr(prepare, "send_draft", fake_send)
    controller._queue_send(drafts)
    deadline = time.time() + 5
    while time.time() < deadline and peak[0] < 3:
        controller.app.processEvents()
        time.sleep(0.02)
    assert peak[0] == 3                                                  # three browsers at once
    release.set()
    deadline = time.time() + 10
    while time.time() < deadline and controller._sending:
        controller.app.processEvents()
        time.sleep(0.02)
    assert not controller._sending and not controller._send_active
    assert all(group.count("Alpha") <= 1 for group in seen_together)    # never two to one employer
    assert controller.window.sidebar.console.last_text.startswith("Finished sending · 5 sent")


def test_busy_sends_wait_their_turn_instead_of_failing(controller, monkeypatch):
    job = posting("Alpha", 1)
    db.upsert_jobs([job])
    draft_id = db.upsert_draft(job.id, status="approved")
    replies = [{"status": "busy", "draft_id": draft_id}, {"status": "applied", "draft_id": draft_id}]
    monkeypatch.setattr(prepare, "send_draft", lambda draft_id, parallel=1, assist=False: replies.pop(0))
    monkeypatch.setattr("jobhub.desktop.controller.QTimer.singleShot", lambda ms, fn: fn())
    controller._queue_send([draft_id])
    deadline = time.time() + 5
    while time.time() < deadline and controller._sending:
        controller.app.processEvents()
        time.sleep(0.02)
    assert not replies and not controller._sending


# ------------------------------------------------------------------ calmer surfaces
def test_inset_panels_are_soft_wells_not_a_second_glass_layer(controller):
    from jobhub.desktop.theme import PALETTES
    from jobhub.desktop.widgets import GlassPanel

    panel = GlassPanel(radius=14, material="inset")
    assert panel._native_glass.active is False
    assert controller.window.apps.draft_detail.questions_panel._material == "inset"
    for palette in PALETTES.values():
        assert "fill_inset" in palette


def test_settings_save_parallel_sends_and_the_browser_choice(controller):
    view = controller.window.settings
    view.set_settings(db.get_settings())
    assert view.parallel_sends.value() == 3 and not view.show_browser.isChecked()
    view.parallel_sends.setValue(6)
    view.show_browser.setChecked(True)
    patches = []
    view.save_requested.connect(patches.append)
    view._save()
    assert patches[-1]["parallel_sends"] == 6 and patches[-1]["show_browser"] is True
    with pytest.raises(ValidationError):
        db.set_settings({"parallel_sends": 50})
