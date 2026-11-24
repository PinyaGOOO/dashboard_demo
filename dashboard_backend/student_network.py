"""Address policy for real, operator-provisioned student stands."""
from __future__ import annotations

import ipaddress
import re


STUDENT_NETWORK = ipaddress.IPv4Network("10.39.0.0/16")


def student_deployment_network(owner: str, subnet: str, start_ip: str) -> tuple[str, str]:
    if not re.fullmatch(r"student[0-9]+", owner.strip().casefold()):
        return subnet, start_ip

    network_error = f"Для студенческого стенда требуется адрес из сети {STUDENT_NETWORK} с маской /16"
    try:
        interface = ipaddress.IPv4Interface(subnet or str(STUDENT_NETWORK))
        start = ipaddress.IPv4Interface(start_ip) if start_ip else interface
    except ValueError as exc:
        raise ValueError(network_error) from exc
    if (interface.network != STUDENT_NETWORK or start.ip not in STUDENT_NETWORK or
            ("/" in start_ip and start.network != STUDENT_NETWORK)):
        raise ValueError(network_error)
    if interface.ip == STUDENT_NETWORK.broadcast_address or start.ip in {
        STUDENT_NETWORK.network_address, STUDENT_NETWORK.broadcast_address,
    }:
        raise ValueError(f"Укажите выделенный начальный адрес студенческих VM в {STUDENT_NETWORK}. "
                         "Адрес сети или широковещательный адрес не подходит; адрес шлюза не выбирается автоматически.")

    # Preserve an explicit operator-selected host part, including e.g. 10.39.4.0.
    return str(interface) if subnet else f"{start.ip}/{STUDENT_NETWORK.prefixlen}", str(start.ip)
