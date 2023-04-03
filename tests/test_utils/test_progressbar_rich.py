# Copyright (c) OpenMMLab. All rights reserved.
import time

import mmengine


class TestRichProgressBar:

    def test_start(self):
        # single task
        prog_bar = mmengine.RichProgressBar()
        prog_bar.add_task(100)
        assert len(prog_bar.bar.tasks) == 1
        assert prog_bar.bar.tasks[0].total == 100
        assert prog_bar.infinite is False
        del prog_bar
        # multi tasks
        prog_bar = mmengine.RichProgressBar()
        for i in range(5):
            prog_bar.add_task(10)
        assert len(prog_bar.bar.tasks) == 5
        del prog_bar
        # without total task num
        prog_bar = mmengine.RichProgressBar()
        prog_bar.add_task(None)
        assert prog_bar.infinite is True
        del prog_bar

    def test_update(self):
        # single task
        prog_bar = mmengine.RichProgressBar()
        prog_bar.add_task(10)
        for i in range(10):
            prog_bar.update()
        assert prog_bar.bar.tasks[0].finished is True
        del prog_bar
        # without total task num
        prog_bar = mmengine.RichProgressBar()
        prog_bar.add_task(None)
        for i in range(10):
            prog_bar.update()
        assert prog_bar.completed == 10
        del prog_bar
        # multi task
        prog_bar = mmengine.RichProgressBar()
        task_ids = []
        for i in range(10):
            task_ids.append(prog_bar.add_task(10))
        for i in range(10):
            for idx in task_ids:
                prog_bar.update(idx)
        assert prog_bar.bar.finished is True
        for idx in task_ids:
            assert prog_bar.bar.tasks[idx].finished is True
        del prog_bar


def sleep_1s(num):
    time.sleep(1)
    return num


def add(x, y):
    time.sleep(1)
    return x + y


def test_track_progress_finite():
    ret = mmengine.tracking(add, ([1, 2, 3], [4, 5, 6]), infinite=False)
    assert ret == [5, 7, 9]


def test_track_progress_infinite():
    ret = mmengine.tracking(add, ([1, 2, 3], [4, 5, 6]), infinite=True)
    assert ret == [5, 7, 9]


def test_track_parallel_progress_finite():
    results = mmengine.tracking(
        add, ([1, 2, 3], [4, 5, 6]), nproc=3, infinite=False)
    assert results == [5, 7, 9]


def test_track_parallel_progress_infinite():
    results = mmengine.tracking(
        add, ([1, 2, 3], [4, 5, 6]), nproc=3, infinite=True)
    assert results == [5, 7, 9]


def test_track_parallel_progress_iterator():
    results = mmengine.tracking(sleep_1s, ([1, 2, 3, 4], ), 4, nproc=3)
    assert results == [1, 2, 3, 4]
