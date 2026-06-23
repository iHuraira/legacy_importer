from legacy_importer.schema import CORE_SCHEMA, inspect_schema


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, *_):
        return None

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return FakeCursor(self.rows)


def test_schema_reports_missing_column():
    rows = [
        (table, column)
        for table, columns in CORE_SCHEMA.items()
        for column in columns
        if not (table == "reads" and column == "checksum")
    ]
    rows.extend((table, "sample_id") for table in (
        "fastqc", "mlst", "quast", "bbmap", "bracken", "amrfinder", "qc1", "qc2",
        "bakta_annotations",
    ))
    report = inspect_schema(FakeConnection(rows))
    assert report["ok"] is False
    assert report["missing_columns"]["reads"] == ["checksum"]


def test_schema_accepts_complete_required_shape():
    rows = [(table, column) for table, columns in CORE_SCHEMA.items() for column in columns]
    rows.extend((table, "sample_id") for table in (
        "fastqc", "mlst", "quast", "bbmap", "bracken", "amrfinder", "qc1", "qc2",
        "bakta_annotations",
    ))
    assert inspect_schema(FakeConnection(rows))["ok"] is True
