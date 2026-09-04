"""Markdown demonstration report with explicit epistemic labels."""

from typing import Iterable, Mapping, Sequence

from .models import OpportunityScore, Recommendation


def _facts(items: Iterable[str]) -> str:
    values = list(items)
    return "\n".join("- FACT: {}".format(item) for item in values) if values else "- FACT: No observed data."


def render_markdown(
    scores: Sequence[OpportunityScore],
    recommendations: Sequence[Recommendation],
    source_counts: Mapping[str, int],
    source_modes: Mapping[str, str],
) -> str:
    ranked = sorted(scores, key=lambda item: (-item.final_score, item.topic))
    by_topic = {item.topic: item for item in recommendations}
    all_local_fixtures = bool(source_modes) and all(
        mode == "local_fixture" for mode in source_modes.values()
    )
    executive_source = (
        "local fixtures" if all_local_fixtures else "configured read-only sources"
    )
    lines = [
        "# Colixo SEO / Market Intelligence Agent V3",
        "",
        "## 1. Executive summary",
        "",
        "- FACT: {} opportunity topic(s) evaluated from {}.".format(
            len(ranked), executive_source
        ),
        "- INFERENCE: Scores summarize known evidence only; unknown metrics are excluded.",
        "- RECOMMENDATION: Human review is required before any action.",
        "",
        "## 2. Search demand",
        "",
        _facts("{}: {}".format(item.topic, item.search_demand.value) for item in ranked),
        "",
        "## 3. Rank opportunities",
        "",
        _facts("{}: {}".format(item.topic, item.rank_opportunity.value) for item in ranked),
        "",
        "## 4. Conversion signals",
        "",
        _facts("{}: {}".format(item.topic, item.conversion_signal.value) for item in ranked),
        "",
        "## 5. Competitive gaps",
        "",
        _facts("{}: {}".format(item.topic, item.competitive_gap.value) for item in ranked),
        "",
        "## 6. Customer reputation signals",
        "",
        _facts("{}: {}".format(item.topic, item.reputation_gap.value) for item in ranked),
        "",
        "## 7. Commercial value",
        "",
        _facts("{}: {}".format(item.topic, item.commercial_fit.value) for item in ranked),
        "",
        "## 8. Recommended actions",
        "",
    ]
    if ranked:
        for score in ranked:
            recommendation = by_topic[score.topic]
            lines.extend(
                [
                    "- INFERENCE: {} scores {}/100 with {} confidence.".format(
                        score.topic, score.final_score, score.confidence.value
                    ),
                    "- RECOMMENDATION: [{}] {}".format(
                        recommendation.strength, recommendation.action
                    ),
                ]
            )
    else:
        lines.append("- RECOMMENDATION: Collect evidence before proposing actions.")
    unknown = [
        "{}: {}".format(item.topic, ", ".join(item.unknown_dimensions))
        for item in ranked
        if item.unknown_dimensions
    ]
    provenance_lines = []
    if all_local_fixtures:
        provenance_lines.append(
            "- FACT: Sources are local fixtures only: {}.".format(
                ", ".join(
                    "{}={}".format(name, count)
                    for name, count in sorted(source_counts.items())
                )
                or "none"
            )
        )
    else:
        rendered_modes = []
        for name, mode in sorted(source_modes.items()):
            rendered_mode = (
                "ga4_data_api(read-only)" if mode == "ga4_data_api" else mode
            )
            rendered_modes.append("{}={}".format(name, rendered_mode))
        provenance_lines.extend(
            [
                "- FACT: Source modes: {}.".format(", ".join(rendered_modes)),
                "- FACT: Source counts: {}.".format(
                    ", ".join(
                        "{}={}".format(name, count)
                        for name, count in sorted(source_counts.items())
                    )
                    or "none"
                ),
            ]
        )
    lines.extend(
        [
            "",
            "## 9. Unknown / insufficient evidence",
            "",
            _facts(unknown),
            "",
            "## 10. Safety / data provenance",
            "",
            *provenance_lines,
            "- FACT: Models contain aggregated metrics and no intentional personal data fields.",
            "- INFERENCE: Evidence confidence limits recommendation strength.",
            "- RECOMMENDATION: Keep V3 read-only and proposal-only until separately authorized.",
            "",
        ]
    )
    return "\n".join(lines)
