from app.production.readiness import readiness_score


def test_readiness_score_all_pass():
    assert readiness_score({"pass": 10, "warn": 0, "fail": 0}) == 100


def test_readiness_score_mixed():
    assert readiness_score({"pass": 7, "warn": 2, "fail": 1}) == 70
