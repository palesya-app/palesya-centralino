"""Layer di compatibilità DB: stesso codice su SQLite (locale/test) e Postgres (Render).

Il servizio usa la sintassi SQLite (placeholder ``?``, ``BEGIN IMMEDIATE``,
righe accessibili per nome case-insensitive come ``sqlite3.Row``). Quando è
configurato un ``DATABASE_URL`` Postgres, questo wrapper traduce al volo e usa
psycopg, così la memoria (ledger, sessioni, idempotenza) è persistente.
"""
import re
import sqlite3


def is_postgres(target):
    value = str(target or "")
    return value.startswith("postgres://") or value.startswith("postgresql://")


def _dsn(url):
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


class _CIRow(dict):
    """Riga con accesso per nome case-insensitive e per indice, come sqlite3.Row."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        try:
            return super().__getitem__(key)
        except KeyError:
            lowered = key.lower()
            for existing, value in self.items():
                if existing.lower() == lowered:
                    return value
            raise


def _ci_row_factory(cursor):
    # Postgres restituisce i nomi colonna in minuscolo; il codice usa MAIUSCOLO
    # (come lo schema e come sqlite3.Row). Normalizziamo così anche dict(row) è
    # coerente. Le espressioni (COUNT, ecc.) restano accessibili per indice.
    columns = [column.name.upper() for column in (cursor.description or [])]

    def make(values):
        return _CIRow(zip(columns, values))

    return make


_PLACEHOLDER = re.compile(r"\?")


def split_statements(script):
    """Spezza uno script SQL in statement, ignorando le righe di commento.

    Lo split ingenuo su ``;`` rompe se un commento ``--`` contiene un punto e
    virgola: il frammento dopo il ``;`` diventerebbe uno statement invalido.
    SQLite non se ne accorge (esegue lo script intero), Postgres sì — quindi
    l'errore si vedrebbe solo in produzione. Le righe di commento vengono tolte
    prima dello split.
    """
    lines = [line for line in str(script or "").splitlines()
             if not line.strip().startswith("--")]
    return [statement for statement in "\n".join(lines).split(";") if statement.strip()]


class _PgResult:
    def __init__(self, cursor):
        self._cursor = cursor

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def rowcount(self):
        return self._cursor.rowcount


class PgConnection:
    """Espone l'API minima di sqlite3.Connection usata dal servizio."""

    def __init__(self, connection):
        self._connection = connection

    @staticmethod
    def _translate(sql):
        sql = sql.replace("BEGIN IMMEDIATE", "BEGIN")
        return _PLACEHOLDER.sub("%s", sql)

    def execute(self, sql, params=()):
        cursor = self._connection.cursor(row_factory=_ci_row_factory)
        cursor.execute(self._translate(sql), tuple(params))
        return _PgResult(cursor)

    def executescript(self, script):
        with self._connection.cursor() as cursor:
            for statement in split_statements(script):
                cursor.execute(statement)
        self._connection.commit()

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


def connect(target, timeout=30):
    """Ritorna una connessione con l'API di sqlite3, verso Postgres o SQLite."""
    if is_postgres(target):
        import psycopg
        connection = psycopg.connect(_dsn(target), connect_timeout=timeout)
        return PgConnection(connection)
    connection = sqlite3.connect(str(target), timeout=timeout)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection
