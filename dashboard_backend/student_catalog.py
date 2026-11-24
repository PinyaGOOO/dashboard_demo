"""Public teaching material. Student requests never call the Proxmox API."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def public_templates(db_path: Path, templates: list[dict]) -> list[dict]:
    """Join published scenario metadata to *currently verified* QEMU templates."""
    names = {int(t["vmid"]): str(t["name"]) for t in templates if t.get("vmid") and t.get("name")}
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT id, name, template_vmid FROM blueprints WHERE status='active' ORDER BY id")
        return [{"blueprint_id": bp, "scenario": title.strip(), "vmid": vmid, "template": names[vmid]}
                for bp, title, vmid in rows if vmid in names]


def _exercise(name: str) -> tuple:
    if "debian-12-network" in name:
        return (0, "Адресный план для сети Debian",
                "Подготовьте схему небольшой лабораторной сети и объясните, как проверить путь от рабочего места до сервера.",
                ["Для учебной сети 192.0.2.0/28 укажите адрес сети, шлюза, двух узлов и широковещательный адрес.",
                 "Для узла Debian запишите ожидаемые значения адреса, префикса и маршрута по умолчанию.",
                 "Подготовьте таблицу проверок до шлюза и до сервера: команда, ожидаемый результат и вывод при ошибке."])
    if "windows-server-2022" in name:
        return (1, "План настройки DNS на Windows Server",
                "Спроектируйте внутреннюю зону DNS для аудитории и подготовьте проверку разрешения имён с рабочего места.",
                ["Для учебной зоны lab.example укажите назначение записей SOA, NS и A.",
                 "Составьте таблицу записей для сервера и учебного веб-сервиса, используя адреса из 192.0.2.0/24.",
                 "Запишите запросы Resolve-DnsName для проверки с рабочего места. Объясните, чем отсутствие записи отличается от недоступности сервера."])
    if "debian-12-security" in name:
        return (2, "Проверка прав доступа в Debian",
                "Составьте модель доступа к каталогу с работами студентов по принципу минимальных привилегий.",
                ["Разделите роли преподавателя и студента: кто может читать, создавать и изменять файлы.",
                 "Для каталога курса и файла ответа предложите владельца, группу и права. Обоснуйте каждый выбор.",
                 "Опишите проверки от имени обеих ролей, включая отказ в изменении чужой работы, и оформите таблицу ожидаемых результатов."])
    if "pr1" in name or "empty" in name:
        return (0, "Паспорт учебного стенда",
                "Составьте паспорт исходной среды, по которому другой администратор сможет определить её назначение и проверить готовность к работе.",
                ["Запишите имя и VMID исходного шаблона, а также имя личного стенда из его карточки.",
                 "Подготовьте таблицу для учёта узлов: роль, интерфейс, адрес, назначение. Неизвестные поля отметьте «требует проверки».",
                 "Опишите три проверки перед началом работы: запуск, доступность сети и вход через веб-интерфейс."])
    if "pr2" in name or "infra" in name:
        return (1, "Проверка готовности инфраструктуры",
                "Подготовьте план приёмки инфраструктурного стенда: от состояния виртуальной машины до доступности сервиса из аудитории.",
                ["Зафиксируйте исходный шаблон и экземпляр стенда. Перечислите компоненты, состояние которых нужно подтвердить.",
                 "Для сети, разрешения имён и веб-интерфейса составьте таблицу: точка проверки, действие, ожидаемый результат.",
                 "Разберите случай «машина запущена, но страница не открывается». Предложите порядок диагностики и критерий готовности."])
    if "mdk" in name or "мдк" in name:
        return (2, "Практикум: карта сети",
                "Спроектируйте адресный план для учебного сегмента и объясните, как проверите связность между его узлами.",
                ["Для условной сети 192.0.2.0/27 укажите адрес сети, допустимый диапазон узлов и широковещательный адрес.",
                 "Назначьте в своём плане адреса шлюзу, серверу и двум рабочим местам. Отметьте, какие адреса нужно зарезервировать.",
                 "Составьте матрицу проверок: рабочее место → шлюз, рабочее место → сервер, сервер → DNS. Для каждой укажите ожидаемый результат."])
    if "module" in name or "модул" in name or "demo" in name:
        if "2027" in name:
            return (4, "Модуль 2027: план восстановления",
                    "Подготовьте план восстановления экзаменационного рабочего места и критерии его повторной приёмки.",
                    ["Укажите, что надо сохранить до восстановления: данные студента, версию шаблона и сведения об ошибке.",
                     "Разберите учебный случай «после восстановления не открывается сервис». Составьте порядок проверок сети, имён и самого сервиса.",
                     "Оформите протокол повторной приёмки: действие, ожидаемый результат и основание вернуть место в работу."])
        return (3, "Модуль 2026: приёмка рабочего места" if "2026" in name else "Приёмка экзаменационного стенда",
                "Оформите короткий протокол, по которому преподаватель сможет отличить работающую VM от полностью готового рабочего места.",
                ["Укажите шаблон из карточки и границы проверки: что видно в Deployer, а что проверяется с компьютера студента.",
                 "Составьте три сценария проверки с ожидаемым результатом, включая один отказ сети.",
                 "Предложите порядок восстановления и повторной проверки. Отдельно укажите, какие пользовательские данные нельзя потерять."])
    return (5, "Исследование учебной среды",
            "Подготовьте краткое описание выбранного шаблона и воспроизводимый план проверки для следующего студента.",
            ["Запишите исходный шаблон, VMID и имя выделенного вам экземпляра.",
             "Укажите назначение среды и возможные зависимости. Разделите известные факты и предположения.",
             "Оформите две проверки в виде «действие → ожидаемый результат» и опишите, как зафиксировать отклонения."])


def make_assignments(rows: list[dict]) -> list[dict]:
    result = []
    for row in rows:
        template, scenario = row["template"], row["scenario"]
        rank, title, description, steps = _exercise((template + " " + scenario).casefold())
        result.append((rank, int(row["blueprint_id"]), {
            "id": f"lab-{int(row['blueprint_id'])}-{int(row['vmid'])}",
            "title": title, "description": description, "steps": steps,
            "template_name": template, "template_vmid": int(row["vmid"]),
        }))
    return [task for _, _, task in sorted(result, key=lambda t: (t[0], t[1]))]


def load_catalog(path: Path, *, example_if_missing: bool = True) -> list[dict]:
    # The standalone example uses the same demo templates as the operator UI.
    # An existing (even empty) snapshot always wins over the offline examples.
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not example_if_missing:
            return []
        rows = [
            {"blueprint_id": 1, "scenario": "Сетевое администрирование", "vmid": 9001, "template": "debian-12-network"},
            {"blueprint_id": 2, "scenario": "Системное администрирование", "vmid": 9002, "template": "windows-server-2022"},
            {"blueprint_id": 3, "scenario": "Информационная безопасность", "vmid": 9003, "template": "debian-12-security"},
        ]
    return make_assignments(rows)


def refresh_catalog(source_db: Path, catalog_path: Path) -> int:
    """Explicit operator action: snapshot public metadata without queueing cluster operations."""
    if not source_db.is_file():
        raise FileNotFoundError(f"База сценариев не найдена: {source_db}")
    if catalog_path.suffix.lower() != ".json" or catalog_path.is_symlink():
        raise ValueError("Каталог должен быть отдельным файлом .json, не ссылкой")
    if catalog_path.exists():
        if source_db.samefile(catalog_path):
            raise ValueError("Нельзя записывать каталог поверх базы сценариев")
        with catalog_path.open("rb") as target:
            if target.read(16) == b"SQLite format 3\x00":
                raise ValueError("Нельзя записывать каталог поверх базы SQLite")

    from .proxmox_gateway import create_gateway

    rows = public_templates(source_db, create_gateway().list_templates())
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the previous verified snapshot if reading or writing the new one fails.
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=catalog_path.parent, prefix=".catalog-", suffix=".tmp",
                                         encoding="utf-8", mode="w", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            json.dump(rows, tmp, ensure_ascii=False, indent=2)
            tmp.write("\n")
        os.replace(tmp_path, catalog_path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return len(rows)
