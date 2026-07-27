"""Pure decision logic for turning face observations into an event verdict."""
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class DecisionPolicy:
    match_threshold: float = 0.50
    unknown_threshold: float = 0.35
    match_margin: float = 0.08
    ignore_threshold: float = 0.50
    ignore_margin: float = 0.12
    min_confirmations: int = 2


@dataclass
class Decision:
    status: str
    person: str | None = None
    slug: str | None = None
    score: float = 0.0
    margin: float = 0.0
    confirmations: int = 0
    runner_up: str | None = None
    runner_up_score: float = 0.0
    ignore_score: float = 0.0


@dataclass
class DecisionAccumulator:
    policy: DecisionPolicy
    observations: list[dict] = field(default_factory=list)
    _keys: set[str] = field(default_factory=set)

    def add(
        self,
        *,
        key: str,
        slug: str | None,
        person: str | None,
        score: float,
        runner_up: str | None,
        runner_up_score: float,
        ignore_score: float,
        ignore_group: str | None,
    ) -> Decision:
        """Add one distinct frame and return the current event decision.

        A high score alone is deliberately insufficient: recognized and ignored
        verdicts require both a margin and repeated agreement on distinct frames.
        """
        margin = score - runner_up_score
        if key in self._keys:
            return Decision(
                status="duplicate",
                person=person,
                slug=slug,
                score=score,
                margin=margin,
                runner_up=runner_up,
                runner_up_score=runner_up_score,
                ignore_score=ignore_score,
            )
        self._keys.add(key)

        ignore_margin = ignore_score - score
        if (
            ignore_group
            and ignore_score >= self.policy.ignore_threshold
            and ignore_margin >= self.policy.ignore_margin
        ):
            status = "ignored_candidate"
        elif (
            slug
            and score >= self.policy.match_threshold
            and margin >= self.policy.match_margin
        ):
            status = "recognized_candidate"
        elif score < self.policy.unknown_threshold:
            status = "unknown"
        else:
            status = "ambiguous"

        observation = {
            "status": status,
            "slug": slug,
            "person": person,
            "score": score,
            "margin": margin,
            "runner_up": runner_up,
            "runner_up_score": runner_up_score,
            "ignore_score": ignore_score,
            "ignore_group": ignore_group,
        }
        self.observations.append(observation)

        if status == "ignored_candidate":
            counts = Counter(
                o["ignore_group"]
                for o in self.observations
                if o["status"] == "ignored_candidate"
            )
            confirmations = counts[ignore_group]
            if confirmations >= self.policy.min_confirmations:
                return Decision(
                    status="ignored",
                    score=ignore_score,
                    margin=ignore_margin,
                    confirmations=confirmations,
                    ignore_score=ignore_score,
                )

        if status == "recognized_candidate":
            counts = Counter(
                o["slug"]
                for o in self.observations
                if o["status"] == "recognized_candidate"
            )
            confirmations = counts[slug]
            if confirmations >= self.policy.min_confirmations:
                matches = [
                    o
                    for o in self.observations
                    if o["status"] == "recognized_candidate" and o["slug"] == slug
                ]
                best = max(matches, key=lambda o: (o["score"], o["margin"]))
                return Decision(
                    status="recognized",
                    person=best["person"],
                    slug=best["slug"],
                    score=best["score"],
                    margin=best["margin"],
                    confirmations=confirmations,
                    runner_up=best["runner_up"],
                    runner_up_score=best["runner_up_score"],
                    ignore_score=best["ignore_score"],
                )

        return Decision(
            status=status,
            person=person,
            slug=slug,
            score=score,
            margin=margin,
            confirmations=1,
            runner_up=runner_up,
            runner_up_score=runner_up_score,
            ignore_score=ignore_score,
        )

    def pending_status(self) -> str:
        if not self.observations:
            return "no_face"
        statuses = {o["status"] for o in self.observations}
        if "recognized_candidate" in statuses or "ignored_candidate" in statuses:
            return "ambiguous"
        return "ambiguous" if "ambiguous" in statuses else "unknown"
