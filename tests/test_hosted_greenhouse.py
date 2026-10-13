"""Greenhouse postings listed on a company's careers page ("?gh_jid=") become preparable.

All network calls are local fakes.
"""
from jobhub.models import Job, job_fingerprint
from jobhub.sources import base


class FakeClient:
    """Greenhouse's embed redirects a known job id to "?for=<board>"; an unknown one returns 404."""
    calls: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url):
        FakeClient.calls.append(url)
        token = url.rsplit("=", 1)[-1]
        boards = {"8247550": "seatgeek", "8451764002": "optiverus"}
        final = f"https://job-boards.greenhouse.io/embed/job_app?for={boards[token]}&token={token}" if token in boards else url
        return type("R", (), {"url": final, "status_code": 200 if token in boards else 404})()


def careers(company: str, url: str) -> Job:
    return Job(source="simplify:newgrad", ats="unknown", company=company, title="Software Engineer", url=url, apply_url=url)


def test_careers_page_links_are_pointed_at_their_greenhouse_board(monkeypatch):
    monkeypatch.setattr(base, "client", FakeClient)
    monkeypatch.setattr(base, "_GREENHOUSE_BOARD_OF", {})
    FakeClient.calls = []
    seatgeek = careers("SeatGeek", "https://seatgeek.com/jobs/8247550?gh_jid=8247550")
    optiver = careers("Optiver", "https://www.optiver.com/join-us/jobs/8451764002/?gh_jid=8451764002")
    closed = careers("Gone", "https://example.test/careers?gh_jid=999")
    workday = careers("Elsewhere", "https://example.wd1.myworkdayjobs.com/en-US/careers/job/123")
    job_id = seatgeek.id

    assert base.resolve_hosted_greenhouse([seatgeek, optiver, closed, workday]) == 2
    assert (seatgeek.ats, seatgeek.apply_url) == ("greenhouse", "https://job-boards.greenhouse.io/seatgeek/jobs/8247550")
    assert optiver.apply_url == "https://job-boards.greenhouse.io/optiverus/jobs/8451764002"
    assert seatgeek.id == job_id   # the saved job keeps its identity; the board link is how it is applied to
    # The same vacancy read from the board itself now counts as one job.
    board_copy = Job(source="greenhouse", ats="greenhouse", company="SeatGeek", title="Software Engineer",
                     url="https://job-boards.greenhouse.io/seatgeek/jobs/8247550")
    assert job_fingerprint(seatgeek) == job_fingerprint(board_copy)
    assert closed.ats == "unknown" and workday.ats == "unknown"

    # Answers are remembered, so the next scan asks Greenhouse nothing.
    asked = len(FakeClient.calls)
    again = careers("SeatGeek", "https://seatgeek.com/jobs/8247550?gh_jid=8247550")
    assert base.resolve_hosted_greenhouse([again, careers("Gone", "https://example.test/careers?gh_jid=999")]) == 1
    assert len(FakeClient.calls) == asked


def test_resolve_hosted_greenhouse_handles_embed_and_grnh_se(monkeypatch):
    class FakeResolverClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            if "grnh.se" in url:
                return type("R", (), {"url": "https://job-boards.greenhouse.io/doordash/jobs/8264542"})()
            return type("R", (), {"url": url, "status_code": 404})()

    monkeypatch.setattr(base, "client", FakeResolverClient)
    embed_with_for = careers("Gemini", "https://boards.greenhouse.io/embed/job_app?for=gemini&token=7951195")
    shortlink = careers("DoorDash", "https://grnh.se/ltx0agxi1us")

    assert base.resolve_hosted_greenhouse([embed_with_for, shortlink]) == 2
    assert (embed_with_for.ats, embed_with_for.apply_url) == ("greenhouse", "https://job-boards.greenhouse.io/gemini/jobs/7951195")
    assert (shortlink.ats, shortlink.apply_url) == ("greenhouse", "https://job-boards.greenhouse.io/doordash/jobs/8264542")
