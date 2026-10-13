from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Fold:
    train: tuple[int, ...]
    validation: tuple[int, ...]
    test: tuple[int, ...]


def walk_forward(
    timestamps: list[datetime],
    label_ends: list[datetime],
    *,
    n_folds: int,
    embargo: timedelta,
) -> list[Fold]:
    """Expanding walk-forward. Train, validation, and test never share a row."""
    count = len(timestamps)
    if n_folds < 2 or count < n_folds * 3:
        return []
    test_size = count // (n_folds + 2)
    if test_size < 1:
        return []
    folds: list[Fold] = []
    for fold in range(n_folds):
        test_end = count - (n_folds - fold - 1) * test_size
        test_start = test_end - test_size
        validation_end = test_start
        validation_start = validation_end - test_size
        if validation_start < test_size:
            continue
        train = tuple(range(validation_start))
        validation = tuple(range(validation_start, validation_end))
        test = tuple(range(test_start, test_end))
        train, validation = purge_and_embargo(timestamps, label_ends, train, validation, test, embargo)
        if train and validation and test and _ordered(train, validation, test):
            folds.append(Fold(train, validation, test))
    return folds


def purge_and_embargo(
    timestamps: list[datetime],
    label_ends: list[datetime],
    train: tuple[int, ...],
    validation: tuple[int, ...],
    test: tuple[int, ...],
    embargo: timedelta,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    validation_start = timestamps[validation[0]]
    test_start = timestamps[test[0]]
    purged_train = []
    for index in train:
        if label_ends[index] > validation_start:
            continue
        if embargo > timedelta(0) and timestamps[index] >= validation_start - embargo:
            continue
        purged_train.append(index)
    purged_validation = []
    for index in validation:
        if label_ends[index] > test_start:
            continue
        if embargo > timedelta(0) and timestamps[index] >= test_start - embargo:
            continue
        purged_validation.append(index)
    return tuple(purged_train), tuple(purged_validation)


def _ordered(train: tuple[int, ...], validation: tuple[int, ...], test: tuple[int, ...]) -> bool:
    if set(train) & set(validation) or set(train) & set(test) or set(validation) & set(test):
        return False
    return max(train) < min(validation) < max(validation) < min(test)
