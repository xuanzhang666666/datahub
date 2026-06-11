from importlib import import_module


def test_scheduler_datajob_sync_modules_are_importable() -> None:
    for module_name in (
        "scheduler_datajob_sync.models",
        "scheduler_datajob_sync.mysql_client",
        "scheduler_datajob_sync.datajob_writer",
        "scheduler_datajob_sync.sync_datajobs",
    ):
        import_module(module_name)
