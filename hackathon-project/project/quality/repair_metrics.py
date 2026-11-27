"""Compute correction metrics from independently labeled and checked objects."""
from collections import Counter


def _rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def labeled_metrics(cases, *, exhaustive):
    """Each case supplies expected_error, detected, saved and correct_after booleans."""
    cases = list(cases)
    errors = [case for case in cases if case["expected_error"]]
    saved = [case for case in cases if case["saved"]]
    true_positives = sum(case["detected"] for case in errors)
    false_positives = sum(not case["expected_error"] and case["detected"] for case in cases)
    false_negatives = len(errors) - true_positives
    corrected = sum(case["saved"] and case["correct_after"] for case in errors)
    incorrect = sum(not case["expected_error"] or not case["correct_after"] for case in saved)
    categories = Counter(case["category"] for case in errors)
    return {
        "exhaustive_labels": exhaustive,
        "labeled_objects": len(cases),
        "labeled_errors": len(errors),
        "errors_by_category": dict(categories),
        "true_positives": true_positives,
        "false_positives": false_positives if exhaustive else None,
        "false_negatives": false_negatives,
        "detection_precision": _rate(true_positives, true_positives + false_positives) if exhaustive else None,
        "detection_recall": _rate(true_positives, len(errors)),
        "detection_f1": _rate(2 * true_positives, 2 * true_positives + false_positives + false_negatives) if exhaustive else None,
        "saved_corrections": len(saved),
        "correct_saved_corrections": corrected,
        "incorrect_saved_corrections": incorrect,
        "remaining_labeled_errors": sum(not case["correct_after"] for case in errors),
        "correction_accuracy": _rate(corrected, len(saved)),
        "correct_after_by_category": {
            category: _rate(sum(case["correct_after"] and case["category"] == category for case in errors), count)
            for category, count in categories.items()
        },
    }
