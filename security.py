"""Autenticazione locale/production senza dipendenze esterne."""
import base64
import datetime as dt
import hashlib
import hmac
import os
import secrets
import sqlite3
import pyotp
from cryptography.fernet import Fernet, InvalidToken

from config import env, settings


PBKDF2_ROUNDS = 390_000
SESSION_HOURS = 12
MAX_LOGIN_FAILURES = 5
LOCKOUT_MINUTES = 15


def _totp_cipher():
    key = hashlib.sha256((settings.secret_key + ":palesya-totp").encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _protect_totp(secret):
    return "enc:" + _totp_cipher().encrypt(secret.encode("ascii")).decode("ascii")


def _unprotect_totp(value):
    if not value or not str(value).startswith("enc:"):
        return value
    try:
        return _totp_cipher().decrypt(str(value)[4:].encode("ascii")).decode("ascii")
    except InvalidToken as exc:
        raise ValueError("Segreto 2FA non decifrabile: verificare PALESYA_SECRET_KEY") from exc


def _conn():
    con = sqlite3.connect(str(settings.database_path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def validate_password_policy(password):
    if len(password) < 10:
        raise ValueError("La password deve contenere almeno 10 caratteri")


def hash_password(password):
    validate_password_policy(password)
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ROUNDS,
        base64.urlsafe_b64encode(salt).decode(),
        base64.urlsafe_b64encode(digest).decode(),
    )


def verify_password(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), base64.urlsafe_b64decode(salt), int(rounds)
        )
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


def create_user(username, password, role="admin", socio_id=None):
    username = username.strip().lower()
    if not username:
        raise ValueError("Username obbligatorio")
    if role not in {"admin", "reception", "instructor", "accounting", "viewer", "customer"}:
        raise ValueError("Ruolo non valido")
    con = _conn()
    try:
        cur = con.execute(
            "INSERT INTO GYMFLOW_USERS(USERNAME,PASSWORD_HASH,ROLE,SOCIO_ID) VALUES(?,?,?,?)",
            (username, hash_password(password), role, socio_id),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def staff_users():
    con = _conn()
    try:
        return [
            dict(item) for item in con.execute(
                """SELECT ID,USERNAME,DISPLAY_NAME,ROLE,ACTIVE,CREATED_AT,LAST_LOGIN_AT,
                          FAILED_LOGIN_COUNT,LOCKED_UNTIL
                     FROM GYMFLOW_USERS WHERE ROLE<>'customer'
                    ORDER BY ACTIVE DESC,DISPLAY_NAME,USERNAME"""
            ).fetchall()
        ]
    finally:
        con.close()


def save_staff_user(username, password, role="reception", display_name="", user_id=None):
    normalized = str(username or "").strip().lower()
    display = " ".join(str(display_name or "").split()).strip()[:120]
    if not normalized:
        raise ValueError("Username obbligatorio")
    if role not in {"admin", "reception", "instructor", "accounting", "viewer"}:
        raise ValueError("Ruolo non valido")
    con = _conn()
    try:
        con.execute("BEGIN IMMEDIATE")
        if user_id:
            existing = con.execute(
                "SELECT ID FROM GYMFLOW_USERS WHERE ID=? AND ROLE<>'customer'",
                (int(user_id),),
            ).fetchone()
            if not existing:
                raise ValueError("Account non trovato")
            duplicate = con.execute(
                "SELECT ID FROM GYMFLOW_USERS WHERE USERNAME=? AND ID<>?",
                (normalized, int(user_id)),
            ).fetchone()
            if duplicate:
                raise ValueError("Username già utilizzato")
            assignments = [
                "USERNAME=?", "DISPLAY_NAME=?", "ROLE=?",
                "FAILED_LOGIN_COUNT=0", "LOCKED_UNTIL=NULL",
            ]
            values = [normalized, display or None, role]
            if password:
                assignments.append("PASSWORD_HASH=?")
                values.append(hash_password(password))
            values.append(int(user_id))
            con.execute(
                "UPDATE GYMFLOW_USERS SET {} WHERE ID=?".format(",".join(assignments)),
                values,
            )
            con.execute("DELETE FROM GYMFLOW_SESSIONS WHERE USER_ID=?", (int(user_id),))
            result = int(user_id)
        else:
            if not password:
                raise ValueError("Password obbligatoria")
            result = int(con.execute(
                """INSERT INTO GYMFLOW_USERS
                   (USERNAME,PASSWORD_HASH,ROLE,DISPLAY_NAME)
                   VALUES(?,?,?,?)""",
                (normalized, hash_password(password), role, display or None),
            ).lastrowid)
        con.commit()
        return result
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def set_staff_active(user_id, active, acting_user_id=None):
    if acting_user_id and int(user_id) == int(acting_user_id) and not active:
        raise ValueError("Non puoi disattivare l'account con cui sei collegato")
    con = _conn()
    try:
        con.execute("BEGIN IMMEDIATE")
        changed = con.execute(
            """UPDATE GYMFLOW_USERS SET ACTIVE=?,FAILED_LOGIN_COUNT=0,LOCKED_UNTIL=NULL
               WHERE ID=? AND ROLE<>'customer'""",
            (int(bool(active)), int(user_id)),
        ).rowcount
        if not changed:
            raise ValueError("Account non trovato")
        if not active:
            con.execute("DELETE FROM GYMFLOW_SESSIONS WHERE USER_ID=?", (int(user_id),))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def customer_user_for_member(socio_id):
    """Ritorna solo i metadati non sensibili dell'accesso portale del socio."""
    con = _conn()
    try:
        row = con.execute(
            """SELECT ID,USERNAME,ACTIVE,CREATED_AT,LAST_LOGIN_AT
               FROM GYMFLOW_USERS
               WHERE ROLE='customer' AND SOCIO_ID=?
               ORDER BY ACTIVE DESC,ID LIMIT 1""",
            (int(socio_id),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def configure_customer_user(socio_id, username, password=""):
    """Crea o aggiorna l'identita portale, revocando le sessioni precedenti.

    La password e obbligatoria alla creazione e facoltativa negli aggiornamenti.
    Un username non puo essere riassegnato a un altro socio o ruolo.
    """
    socio_id = int(socio_id)
    normalized = str(username or "").strip().lower()
    if not normalized:
        raise ValueError("Username obbligatorio")
    if len(normalized) > 120:
        raise ValueError("Username troppo lungo")
    con = _conn()
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            """SELECT ID,USERNAME FROM GYMFLOW_USERS
               WHERE ROLE='customer' AND SOCIO_ID=? ORDER BY ACTIVE DESC,ID LIMIT 1""",
            (socio_id,),
        ).fetchone()
        collision = con.execute(
            "SELECT ID,ROLE,SOCIO_ID FROM GYMFLOW_USERS WHERE USERNAME=?",
            (normalized,),
        ).fetchone()
        if collision and (not existing or int(collision["ID"]) != int(existing["ID"])):
            raise ValueError("Username gia utilizzato")

        if existing:
            values = [normalized]
            assignments = ["USERNAME=?", "ACTIVE=1", "FAILED_LOGIN_COUNT=0", "LOCKED_UNTIL=NULL"]
            if password:
                assignments.append("PASSWORD_HASH=?")
                values.append(hash_password(password))
            values.append(int(existing["ID"]))
            con.execute(
                "UPDATE GYMFLOW_USERS SET {} WHERE ID=?".format(",".join(assignments)),
                tuple(values),
            )
            user_id = int(existing["ID"])
        else:
            if not password:
                raise ValueError("Password obbligatoria per il primo accesso")
            cur = con.execute(
                """INSERT INTO GYMFLOW_USERS(USERNAME,PASSWORD_HASH,ROLE,SOCIO_ID)
                   VALUES(?,?,'customer',?)""",
                (normalized, hash_password(password), socio_id),
            )
            user_id = int(cur.lastrowid)

        con.execute("DELETE FROM GYMFLOW_SESSIONS WHERE USER_ID=?", (user_id,))
        con.commit()
        return user_id
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def disable_customer_user(socio_id):
    """Disabilita il portale del solo socio selezionato e revoca le sessioni."""
    con = _conn()
    try:
        con.execute("BEGIN IMMEDIATE")
        user_ids = [
            int(row["ID"]) for row in con.execute(
                "SELECT ID FROM GYMFLOW_USERS WHERE ROLE='customer' AND SOCIO_ID=?",
                (int(socio_id),),
            ).fetchall()
        ]
        if user_ids:
            placeholders = ",".join("?" for _ in user_ids)
            con.execute(
                "UPDATE GYMFLOW_USERS SET ACTIVE=0 WHERE ID IN ({})".format(placeholders),
                tuple(user_ids),
            )
            con.execute(
                "DELETE FROM GYMFLOW_SESSIONS WHERE USER_ID IN ({})".format(placeholders),
                tuple(user_ids),
            )
        con.commit()
        return len(user_ids)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def validate_admin_password_reset(username, password):
    """Valida il target prima di consumare un codice remoto monouso."""
    normalized = str(username or "").strip().lower()
    validate_password_policy(password)
    con = _conn()
    try:
        row = con.execute(
            "SELECT ID FROM GYMFLOW_USERS WHERE USERNAME=? AND ROLE='admin' AND ACTIVE=1",
            (normalized,),
        ).fetchone()
        if not row:
            raise ValueError("Amministratore locale non trovato")
        return int(row["ID"])
    finally:
        con.close()


def reset_admin_password(username, password):
    """Sostituisce l'hash e revoca tutte le sessioni dell'amministratore locale."""
    normalized = str(username or "").strip().lower()
    encoded = hash_password(password)
    con = _conn()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT ID FROM GYMFLOW_USERS WHERE USERNAME=? AND ROLE='admin' AND ACTIVE=1",
            (normalized,),
        ).fetchone()
        if not row:
            raise ValueError("Amministratore locale non trovato")
        con.execute(
            """UPDATE GYMFLOW_USERS SET PASSWORD_HASH=?,FAILED_LOGIN_COUNT=0,LOCKED_UNTIL=NULL
               WHERE ID=?""",
            (encoded, row["ID"]),
        )
        con.execute("DELETE FROM GYMFLOW_SESSIONS WHERE USER_ID=?", (row["ID"],))
        con.execute(
            """INSERT INTO GYMFLOW_AUDIT(USER_ID,ACTION,ENTITY_TYPE,ENTITY_ID,DETAIL)
               VALUES(?, 'auth.password_reset', 'user', ?, 'support_code')""",
            (row["ID"], str(row["ID"])),
        )
        con.commit()
        return int(row["ID"])
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def user_count():
    """Numero di identita locali configurate, usato dal bootstrap iniziale."""
    con = _conn()
    try:
        return int(con.execute("SELECT COUNT(*) FROM GYMFLOW_USERS").fetchone()[0])
    finally:
        con.close()


def authenticate(username, password):
    con = _conn()
    try:
        row = con.execute(
            "SELECT * FROM GYMFLOW_USERS WHERE USERNAME=? AND ACTIVE=1",
            (username.strip().lower(),),
        ).fetchone()
        if not row:
            return None
        now = dt.datetime.now(dt.timezone.utc)
        locked_until = row["LOCKED_UNTIL"]
        if locked_until:
            locked = dt.datetime.fromisoformat(locked_until)
            if locked.tzinfo is None:
                locked = locked.replace(tzinfo=dt.timezone.utc)
            if now < locked:
                return None
        if not verify_password(password, row["PASSWORD_HASH"]):
            failures = int(row["FAILED_LOGIN_COUNT"] or 0) + 1
            lock = now + dt.timedelta(minutes=LOCKOUT_MINUTES) if failures >= MAX_LOGIN_FAILURES else None
            con.execute(
                "UPDATE GYMFLOW_USERS SET FAILED_LOGIN_COUNT=?,LOCKED_UNTIL=? WHERE ID=?",
                (failures, lock.isoformat() if lock else None, row["ID"]),
            )
            con.commit()
            return None
        con.execute(
            """UPDATE GYMFLOW_USERS SET LAST_LOGIN_AT=CURRENT_TIMESTAMP,
               FAILED_LOGIN_COUNT=0,LOCKED_UNTIL=NULL WHERE ID=?""", (row["ID"],)
        )
        con.commit()
        return dict(con.execute("SELECT * FROM GYMFLOW_USERS WHERE ID=?", (row["ID"],)).fetchone())
    finally:
        con.close()


def new_session(user_id, mfa_verified=True):
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    csrf = secrets.token_urlsafe(24)
    now = dt.datetime.now(dt.timezone.utc)
    expires = now + dt.timedelta(hours=SESSION_HOURS)
    con = _conn()
    try:
        con.execute(
            """INSERT INTO GYMFLOW_SESSIONS
               (TOKEN_HASH,USER_ID,CSRF_TOKEN,CREATED_AT,EXPIRES_AT,MFA_VERIFIED)
               VALUES(?,?,?,?,?,?)""",
            (token_hash, user_id, csrf, now.isoformat(), expires.isoformat(), int(bool(mfa_verified))),
        )
        con.commit()
    finally:
        con.close()
    return token, csrf


def session_user(token):
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    con = _conn()
    try:
        row = con.execute(
            """SELECT u.ID,u.USERNAME,u.ROLE,u.SOCIO_ID,u.TOTP_ENABLED,
                      s.CSRF_TOKEN,s.EXPIRES_AT,s.MFA_VERIFIED
               FROM GYMFLOW_SESSIONS s JOIN GYMFLOW_USERS u ON u.ID=s.USER_ID
               WHERE s.TOKEN_HASH=? AND s.EXPIRES_AT>? AND u.ACTIVE=1""",
            (token_hash, now),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def end_session(token):
    if not token:
        return
    con = _conn()
    try:
        con.execute("DELETE FROM GYMFLOW_SESSIONS WHERE TOKEN_HASH=?", (hashlib.sha256(token.encode()).hexdigest(),))
        con.commit()
    finally:
        con.close()


def bootstrap_admin_from_env():
    username = env("ADMIN_USER", "").strip()
    password = env("ADMIN_PASSWORD", "")
    if user_count() == 0 and username and password:
        user_id = create_user(username, password, "admin")
        configured_secret = env("ADMIN_TOTP_SECRET", "").strip().replace(" ", "")
        if configured_secret:
            set_totp_secret(user_id, configured_secret, enabled=True)
        return user_id
    return None


def user_by_id(user_id):
    con = _conn()
    try:
        row = con.execute("SELECT * FROM GYMFLOW_USERS WHERE ID=? AND ACTIVE=1", (user_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["TOTP_SECRET"] = _unprotect_totp(result.get("TOTP_SECRET"))
        return result
    finally:
        con.close()


def set_totp_secret(user_id, secret=None, enabled=False):
    secret = (secret or pyotp.random_base32()).strip().replace(" ", "").upper()
    pyotp.TOTP(secret).now()  # valida formato Base32 prima di salvarlo
    con = _conn()
    try:
        con.execute(
            "UPDATE GYMFLOW_USERS SET TOTP_SECRET=?,TOTP_ENABLED=? WHERE ID=?",
            (_protect_totp(secret), int(bool(enabled)), user_id),
        )
        con.commit()
        return secret
    finally:
        con.close()


def totp_provisioning_uri(user_id, issuer="Palesya Control"):
    user = user_by_id(user_id)
    if not user:
        raise ValueError("Utente non trovato")
    secret = user.get("TOTP_SECRET") or set_totp_secret(user_id)
    return pyotp.TOTP(secret).provisioning_uri(name=user["USERNAME"], issuer_name=issuer)


def _recovery_hash(code):
    return hashlib.sha256((settings.secret_key + ":" + code.strip().upper()).encode("utf-8")).hexdigest()


def enable_totp(user_id, code):
    user = user_by_id(user_id)
    if not user or not user.get("TOTP_SECRET") or not pyotp.TOTP(user["TOTP_SECRET"]).verify(
        str(code or "").replace(" ", ""), valid_window=1
    ):
        raise ValueError("Codice di verifica non valido")
    recovery_codes = ["{}-{}".format(secrets.token_hex(2).upper(), secrets.token_hex(2).upper()) for _ in range(8)]
    con = _conn()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE GYMFLOW_USERS SET TOTP_ENABLED=1 WHERE ID=?", (user_id,))
        con.execute("DELETE FROM GYMFLOW_MFA_RECOVERY_CODES WHERE USER_ID=?", (user_id,))
        con.executemany(
            "INSERT INTO GYMFLOW_MFA_RECOVERY_CODES(USER_ID,CODE_HASH) VALUES(?,?)",
            [(user_id, _recovery_hash(value)) for value in recovery_codes],
        )
        con.commit()
        return recovery_codes
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def verify_second_factor(user_id, code):
    user = user_by_id(user_id)
    value = str(code or "").strip().replace(" ", "")
    if not user or not user.get("TOTP_ENABLED") or not value:
        return False
    if pyotp.TOTP(user["TOTP_SECRET"]).verify(value, valid_window=1):
        return True
    digest = _recovery_hash(value)
    con = _conn()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """SELECT ID FROM GYMFLOW_MFA_RECOVERY_CODES
               WHERE USER_ID=? AND CODE_HASH=? AND USED_AT IS NULL""", (user_id, digest)
        ).fetchone()
        if not row:
            con.rollback()
            return False
        con.execute("UPDATE GYMFLOW_MFA_RECOVERY_CODES SET USED_AT=CURRENT_TIMESTAMP WHERE ID=?", (row["ID"],))
        con.commit()
        return True
    finally:
        con.close()


def mark_session_mfa(token):
    if not token:
        return
    con = _conn()
    try:
        con.execute(
            "UPDATE GYMFLOW_SESSIONS SET MFA_VERIFIED=1 WHERE TOKEN_HASH=?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        )
        con.commit()
    finally:
        con.close()


def audit(action, entity_type=None, entity_id=None, detail=None, user_id=None, ip=None):
    con = _conn()
    try:
        con.execute(
            "INSERT INTO GYMFLOW_AUDIT(USER_ID,ACTION,ENTITY_TYPE,ENTITY_ID,DETAIL,IP_ADDRESS) VALUES(?,?,?,?,?,?)",
            (user_id, action, entity_type, str(entity_id) if entity_id is not None else None, detail, ip),
        )
        con.commit()
    finally:
        con.close()


ROLE_PERMISSIONS = {
    "admin": {"*"},
    "reception": {"dashboard", "members", "subscriptions", "finance", "access", "crm", "booking", "inventory", "tutorial"},
    "instructor": {"members:read", "booking", "training", "tutorial"},
    "accounting": {"dashboard", "finance", "reports", "tutorial"},
    "viewer": {"dashboard:read", "members:read", "subscriptions:read", "finance:read",
               "access:read", "crm:read", "booking:read", "training:read",
               "inventory:read", "reports:read", "tutorial"},
    "customer": {"portal"},
}


def allowed(user, permission):
    if not settings.auth_required:
        return True
    if not user:
        return False
    permissions = ROLE_PERMISSIONS.get(user.get("ROLE"), set())
    base = permission.split(":", 1)[0]
    return "*" in permissions or permission in permissions or base in permissions
