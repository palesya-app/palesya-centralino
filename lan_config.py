"""Configurazione e diagnostica del nodo Palesya sulla rete locale."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import socket
import tempfile
import time
from urllib import error as urlerror
from urllib import request as urlrequest


CONFIG_NAME = "lan-config.json"
DEFAULT_PORT = 8080
FALLBACK_PORTS = tuple(range(8081, 8101))
_STATUS_CACHE = {"key": None, "at": 0.0, "value": None}


def _private_ipv4(value):
    try:
        address = ipaddress.ip_address(str(value or "").split("%", 1)[0])
    except ValueError:
        return None
    if address.version != 4 or address.is_loopback or address.is_link_local or not address.is_private:
        return None
    return str(address)


def is_loopback(value):
    try:
        address = ipaddress.ip_address(str(value or "").split("%", 1)[0])
        if getattr(address, "ipv4_mapped", None):
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return str(value or "").strip().lower() in {"localhost", "testclient"}


def discover_ipv4():
    """Individua l'IPv4 privato usato dalla route primaria senza inviare dati."""
    candidates = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1: connect UDP non invia pacchetti.
        candidates.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    direct = next((address for address in (_private_ipv4(item) for item in candidates) if address), None)
    if direct:
        return direct
    try:
        candidates.extend(item[4][0] for item in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM
        ))
    except OSError:
        pass
    return next((address for address in (_private_ipv4(item) for item in candidates) if address), None)


def _address_assigned(value):
    """Verifica su Windows che un IP salvato appartenga ancora al PC.

    Una configurazione statica obsoleta non deve continuare a produrre un link
    LAN irraggiungibile dopo un cambio router o scheda di rete.
    """
    address = _private_ipv4(value)
    if not address or os.name != "nt":
        return bool(address)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((address, 0))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _access_ip(payload, current):
    configured = _private_ipv4(payload.get("static_ip"))
    if configured and _address_assigned(configured):
        return configured
    persisted = _private_ipv4(payload.get("current_ip"))
    if persisted and _address_assigned(persisted):
        return persisted
    return _private_ipv4(current)


def config_path(data_dir):
    return Path(data_dir) / CONFIG_NAME


def load(data_dir):
    primary = config_path(data_dir)
    candidates = [primary]
    # Gli aggiornamenti mantengono correttamente il vecchio data-dir GymFlow,
    # mentre l'installer recente salva la configurazione LAN in Palesya. Leggere
    # entrambi evita il falso warning senza cambiare IP o configurazione di rete.
    parent = Path(data_dir).parent
    for folder in ("Palesya", "GymFlow"):
        candidate = parent / folder / CONFIG_NAME
        if candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        try:
            value = json.loads(candidate.read_text(encoding="utf-8-sig"))
            if isinstance(value, dict):
                if candidate != primary:
                    try:
                        _atomic_write(primary, value)
                    except OSError:
                        pass
                return value
        except (OSError, ValueError, TypeError):
            continue
    return {}


def _atomic_write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Un nome fisso come ``lan-config.json.tmp`` può restare bloccato da una
    # precedente istanza Windows o ereditare ACL non scrivibili. Ogni writer
    # usa quindi un temporaneo proprio: un residuo non può più impedire l'avvio.
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(path.name), suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _valid_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1024 <= port <= 65535 else None


def palesya_service_ready(host, port, timeout=0.4):
    """Distingue Palesya da ForFit senza scandire l'intero database.

    ``/ready`` e' intenzionalmente il primo controllo: sulle copie ForFit grandi
    il vecchio ``PRAGMA integrity_check`` di ``/health`` poteva durare diversi
    secondi e far dichiarare spento un server gia avviato. ``/health`` resta il
    fallback di compatibilita per le release Palesya precedenti.
    """
    for path in ("ready", "health"):
        try:
            endpoint = "http://{}:{}/{}".format(host, int(port), path)
            with urlrequest.urlopen(endpoint, timeout=timeout) as response:
                payload = json.loads(response.read(65536).decode("utf-8"))
        except (OSError, ValueError, TypeError, urlerror.URLError):
            continue
        if not isinstance(payload, dict):
            continue
        status = str(payload.get("status", "")).lower()
        if payload.get("product") == "Palesya" and status in {"ok", "ready"}:
            return True
        # Compatibilita con il vecchio /health Palesya, che non esponeva ancora
        # il marker prodotto ma includeva sempre lo stato del database.
        if path == "health" and status == "ok" and "database" in payload:
            return True
    return False


def port_available(host, port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def select_runtime_port(data_dir, preferred=DEFAULT_PORT, system=None, wait_seconds=1.5):
    """Sceglie una porta stabile senza interferire con ForFit.

    La porta salvata dall'installer ha priorita. Se e occupata da un servizio
    diverso da Palesya viene scelta e memorizzata la prima alternativa libera.
    """
    payload = load(data_dir)
    configured = _valid_port(payload.get("port"))
    preferred = _valid_port(preferred) or DEFAULT_PORT
    candidates = []
    # La porta richiesta dall'installer e' il contratto stabile con terminali
    # LAN, browser e PWA. Una vecchia fallback (per esempio 8082) viene
    # riutilizzata soltanto se ospita ancora un nodo Palesya vivo; dopo un
    # aggiornamento pulito si torna invece a 8080.
    for candidate in (preferred, configured, *FALLBACK_PORTS):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    # Prima di scegliere una porta libera, riusa qualunque nodo Palesya già
    # attivo. Questo è essenziale quando il metadata locale conserva una porta
    # fallback ormai libera (es. 8082) ma il nodo stabile continua su 8080.
    # L'ordine mantiene la preferenza per la porta configurata quando esiste
    # davvero, senza creare una seconda istanza.
    for candidate in candidates:
        if palesya_service_ready("127.0.0.1", candidate):
            selected = candidate
            reason = "PALESYA_RUNNING"
            break
    else:
        selected = None

    occupied = []
    if selected is not None:
        try:
            metadata = ensure_runtime_metadata(data_dir, selected, system)
            metadata["runtime_port_status"] = reason
            metadata["runtime_port_conflicts"] = occupied
            _atomic_write(config_path(data_dir), metadata)
        except OSError:
            # Il metadata LAN è ricostruibile e non deve impedire l'uso locale.
            pass
        return selected

    for candidate in candidates:
        if port_available("0.0.0.0", candidate):
            selected = candidate
            reason = "CONFIGURED" if candidate == configured else (
                "PREFERRED" if candidate == preferred else "CONFLICT_FALLBACK"
            )
            break
        occupied.append(candidate)
        # Un secondo avvio Palesya potrebbe avere appena riservato la porta ma
        # non avere ancora pubblicato /health. Gli concediamo un breve margine.
        if candidate == configured and wait_seconds > 0:
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                if palesya_service_ready("127.0.0.1", candidate):
                    selected = candidate
                    reason = "PALESYA_STARTING"
                    break
                time.sleep(0.1)
            else:
                continue
            break
    else:
        raise RuntimeError(
            "Nessuna porta locale disponibile per Palesya (porte verificate: {})".format(
                ", ".join(str(item) for item in candidates)
            )
        )

    try:
        metadata = ensure_runtime_metadata(data_dir, selected, system)
        metadata["runtime_port_status"] = reason
        metadata["runtime_port_conflicts"] = occupied
        _atomic_write(config_path(data_dir), metadata)
    except OSError:
        # Windows può stare ancora rilasciando il file della precedente
        # istanza: la porta scelta resta valida e il server deve partire.
        pass
    return selected


def ensure_runtime_metadata(data_dir, port=8080, system=None):
    """Mantiene il riferimento LAN senza sovrascrivere lo snapshot Windows."""
    path = config_path(data_dir)
    payload = load(data_dir)
    current = discover_ipv4()
    if not payload:
        payload = {
            "schema": 1,
            "status": "DYNAMIC_FALLBACK" if current else "LAN_NOT_DETECTED",
            "mode": "dynamic",
            "platform": system or os.name,
        }
    payload["current_ip"] = current
    payload["port"] = int(port)
    access_ip = _access_ip(payload, current)
    payload["url"] = "http://{}:{}".format(access_ip, int(port)) if access_ip else None
    _atomic_write(path, payload)
    return payload


def status(data_dir, port=8080):
    path = config_path(data_dir)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = 0
    key = (str(Path(data_dir).resolve()), int(port), stamp)
    now = time.monotonic()
    if _STATUS_CACHE["key"] == key and now - _STATUS_CACHE["at"] < 30:
        return dict(_STATUS_CACHE["value"])
    payload = load(data_dir)
    current = discover_ipv4()
    configured = _private_ipv4(payload.get("static_ip"))
    configured_active = bool(configured and _address_assigned(configured))
    access_ip = _access_ip(payload, current)
    mode = str(payload.get("mode") or ("static" if configured else "dynamic")).lower()
    if configured and not configured_active:
        mode = "dynamic"
    result = {
        "status": (
            "STALE_STATIC_FALLBACK" if configured and not configured_active
            else payload.get("status") or ("STATIC_CONFIGURED" if configured else "DYNAMIC_FALLBACK")
        ),
        "mode": mode,
        "static": bool(configured_active and mode == "static"),
        "ip": access_ip,
        "port": int(payload.get("port") or port),
        "url": "http://{}:{}".format(access_ip, int(payload.get("port") or port)) if access_ip else None,
        "interface": payload.get("interface_alias"),
        "network_category": payload.get("network_category"),
        "firewall_verified": bool(payload.get("firewall_verified")),
        "firewall_scope": payload.get("firewall_remote_address"),
        "error": payload.get("error"),
    }
    _STATUS_CACHE.update(key=key, at=now, value=result)
    return dict(result)
