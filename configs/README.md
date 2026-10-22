# configs/

I/O address configs (JSON), one file per named endpoint. Loaded via the CLI's
``--input_address`` / ``--output_address`` flags. Format::

    {
        "src_name": "disc",
        "src_args": { "path": "/data/inboxarchive", "format": "csv" }
    }

Supported ``src_name`` values (populated during P2):

- ``disc`` — local/mounted filesystem CSV/Parquet
- ``snowflake``
- ``databricks``
- ``mssql``, ``oracle``, ``postgres``

**Never** commit credentials here. Credentials live in
``$XDG_CONFIG_HOME/ovid2/secrets.d/*.json`` (mode ``0600``).
