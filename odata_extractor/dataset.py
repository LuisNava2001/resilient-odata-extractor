"""Entity definitions and the seeded synthetic dataset for a fictional equipment-maintenance company.

Everything here is invented: Halcyon Equipment Services does not exist, names are combined from short word lists,
emails use the reserved example.org domain and phone numbers use the fictional 555-01xx range.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

COMPANY = "Halcyon Equipment Services (fictional)"
DEFAULT_SEED = 20260115
START = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Field:
    name: str
    edm_type: str
    nullable: bool = True


@dataclass(frozen=True)
class Navigation:
    name: str
    target_set: str
    foreign_key: str


@dataclass(frozen=True)
class EntitySet:
    name: str
    type_name: str
    key: str
    fields: tuple[Field, ...]
    navigations: tuple[Navigation, ...] = field(default_factory=tuple)

    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]


def _f(name: str, edm: str, nullable: bool = True) -> Field:
    return Field(name, edm, nullable)


ENTITY_SETS: dict[str, EntitySet] = {s.name: s for s in (
    EntitySet("Locations", "Location", "LocationId", (
        _f("LocationId", "Edm.Int32", False), _f("Name", "Edm.String", False), _f("City", "Edm.String", False),
        _f("Region", "Edm.String", False), _f("CountryCode", "Edm.String", False))),
    EntitySet("Technicians", "Technician", "TechnicianId", (
        _f("TechnicianId", "Edm.Int32", False), _f("FullName", "Edm.String", False), _f("Email", "Edm.String", False),
        _f("Phone", "Edm.String"), _f("EmployeeNumber", "Edm.String", False), _f("Skill", "Edm.String", False),
        _f("HomeLocationId", "Edm.Int32", False)),
        (Navigation("HomeLocation", "Locations", "HomeLocationId"),)),
    EntitySet("Equipment", "EquipmentItem", "EquipmentId", (
        _f("EquipmentId", "Edm.Int32", False), _f("AssetTag", "Edm.String", False), _f("Category", "Edm.String", False),
        _f("Model", "Edm.String", False), _f("LocationId", "Edm.Int32", False), _f("InstalledOn", "Edm.Date", False),
        _f("Status", "Edm.String", False)),
        (Navigation("Location", "Locations", "LocationId"),)),
    EntitySet("MaintenanceRequests", "MaintenanceRequest", "RequestId", (
        _f("RequestId", "Edm.Int32", False), _f("EquipmentId", "Edm.Int32", False), _f("RequestedByName", "Edm.String", False),
        _f("RequestedByEmail", "Edm.String", False), _f("Priority", "Edm.String", False), _f("Summary", "Edm.String", False),
        _f("Notes", "Edm.String"), _f("CreatedAt", "Edm.DateTimeOffset", False)),
        (Navigation("Equipment", "Equipment", "EquipmentId"),)),
    EntitySet("WorkOrders", "WorkOrder", "WorkOrderId", (
        _f("WorkOrderId", "Edm.Int32", False), _f("RequestId", "Edm.Int32", False), _f("EquipmentId", "Edm.Int32", False),
        _f("TechnicianId", "Edm.Int32", False), _f("Status", "Edm.String", False), _f("ScheduledFor", "Edm.DateTimeOffset", False),
        _f("CompletedAt", "Edm.DateTimeOffset"), _f("LaborMinutes", "Edm.Int32", False), _f("CostCents", "Edm.Int32", False)),
        (Navigation("Request", "MaintenanceRequests", "RequestId"), Navigation("Equipment", "Equipment", "EquipmentId"),
         Navigation("Technician", "Technicians", "TechnicianId"))),
    EntitySet("ServiceEvents", "ServiceEvent", "ServiceEventId", (
        _f("ServiceEventId", "Edm.Int32", False), _f("WorkOrderId", "Edm.Int32", False), _f("TechnicianId", "Edm.Int32", False),
        _f("EventType", "Edm.String", False), _f("OccurredAt", "Edm.DateTimeOffset", False), _f("Notes", "Edm.String")),
        (Navigation("WorkOrder", "WorkOrders", "WorkOrderId"), Navigation("Technician", "Technicians", "TechnicianId"))),
)}

SIZES = {"Locations": 12, "Technicians": 28, "Equipment": 140, "MaintenanceRequests": 320, "WorkOrders": 420,
         "ServiceEvents": 960}

FIRST = ["Avery", "Blake", "Casey", "Devon", "Elliot", "Frankie", "Harper", "Jordan", "Kendall", "Logan", "Morgan",
         "Noel", "Parker", "Quinn", "Reese", "Rowan", "Sage", "Taylor"]
LAST = ["Ashdown", "Bramley", "Calloway", "Dunmore", "Everly", "Fairbank", "Greaves", "Holloway", "Ingram", "Kettering",
        "Lowell", "Marsden", "Norcott", "Pendry", "Radley", "Stanton", "Thorne", "Wexley"]
CITIES = [("Port Calder", "North", "US"), ("Ashfield", "North", "US"), ("Brimvale", "Central", "US"),
          ("Lake Orrin", "Central", "CA"), ("Westmarch", "West", "US"), ("Saltmere", "West", "CA")]
CATEGORIES = {"Compressor": ["AC-200", "AC-350"], "Forklift": ["FL-12", "FL-18E"], "HVAC unit": ["HV-5T", "HV-10T"],
              "Generator": ["GN-60", "GN-120"], "Conveyor": ["CV-Line", "CV-Flex"], "Pump": ["PM-3", "PM-7"]}
SKILLS = ["Electrical", "Mechanical", "HVAC", "Hydraulics", "Controls"]
PRIORITIES = ["Low", "Medium", "High", "Critical"]
SUMMARIES = ["Unusual noise during startup", "Pressure below threshold", "Belt alignment drift", "Overheating alarm",
             "Scheduled inspection due", "Leak near inlet valve", "Intermittent power loss", "Sensor reading unstable"]
EVENT_TYPES = ["Arrived", "Diagnosed", "PartReplaced", "Tested", "Closed"]


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def generate(seed: int = DEFAULT_SEED) -> dict[str, list[dict[str, Any]]]:
    """Return every entity set as a list of records ordered by key. Same seed, same bytes."""
    rng = random.Random(seed)
    locations = []
    for i in range(1, SIZES["Locations"] + 1):
        city, region, country = CITIES[(i - 1) % len(CITIES)]
        locations.append({"LocationId": i, "Name": f"{city} Depot {((i - 1) // len(CITIES)) + 1}", "City": city,
                          "Region": region, "CountryCode": country})
    technicians = []
    for i in range(1, SIZES["Technicians"] + 1):
        first, last = FIRST[(i * 7) % len(FIRST)], LAST[(i * 5) % len(LAST)]
        technicians.append({
            "TechnicianId": i, "FullName": f"{first} {last}", "Email": f"{first.lower()}.{last.lower()}{i}@example.org",
            "Phone": f"+1-555-01{i:02d}" if rng.random() > 0.2 else None, "EmployeeNumber": f"EMP-{10000 + i * 37}",
            "Skill": rng.choice(SKILLS), "HomeLocationId": rng.randint(1, SIZES["Locations"])})
    equipment = []
    for i in range(1, SIZES["Equipment"] + 1):
        category = rng.choice(sorted(CATEGORIES))
        installed = START - timedelta(days=rng.randint(200, 3000))
        equipment.append({"EquipmentId": i, "AssetTag": f"EQ-{i:05d}", "Category": category,
                          "Model": rng.choice(CATEGORIES[category]), "LocationId": rng.randint(1, SIZES["Locations"]),
                          "InstalledOn": installed.strftime("%Y-%m-%d"),
                          "Status": rng.choices(["Active", "UnderRepair", "Retired"], [80, 15, 5])[0]})
    requests = []
    for i in range(1, SIZES["MaintenanceRequests"] + 1):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        created = START + timedelta(hours=rng.randint(0, 24 * 150))
        note = None
        roll = rng.random()
        if roll < 0.25:
            note = f"Call back {first} on +1-555-01{rng.randint(0, 99):02d} before arrival."
        elif roll < 0.45:
            note = f"Copy {first.lower()}.{last.lower()}@example.org on the completion report."
        elif roll < 0.7:
            note = "Access through the loading bay."
        requests.append({"RequestId": i, "EquipmentId": rng.randint(1, SIZES["Equipment"]),
                         "RequestedByName": f"{first} {last}", "RequestedByEmail": f"{first.lower()}.{last.lower()}@example.org",
                         "Priority": rng.choices(PRIORITIES, [30, 40, 22, 8])[0], "Summary": rng.choice(SUMMARIES),
                         "Notes": note, "CreatedAt": _iso(created)})
    work_orders = []
    for i in range(1, SIZES["WorkOrders"] + 1):
        request = requests[(i * 3) % len(requests)]
        scheduled = datetime.strptime(request["CreatedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) + timedelta(hours=rng.randint(2, 96))
        status = rng.choices(["Open", "InProgress", "Completed", "Cancelled"], [15, 15, 62, 8])[0]
        completed = _iso(scheduled + timedelta(minutes=rng.randint(30, 600))) if status == "Completed" else None
        minutes = rng.randint(20, 480) if status != "Open" else 0
        work_orders.append({"WorkOrderId": i, "RequestId": request["RequestId"], "EquipmentId": request["EquipmentId"],
                            "TechnicianId": rng.randint(1, SIZES["Technicians"]), "Status": status,
                            "ScheduledFor": _iso(scheduled), "CompletedAt": completed, "LaborMinutes": minutes,
                            "CostCents": minutes * rng.choice([95, 110, 125]) * 100 // 60})
    events = []
    for i in range(1, SIZES["ServiceEvents"] + 1):
        order = work_orders[(i * 7) % len(work_orders)]
        occurred = datetime.strptime(order["ScheduledFor"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) + timedelta(minutes=rng.randint(0, 720))
        events.append({"ServiceEventId": i, "WorkOrderId": order["WorkOrderId"], "TechnicianId": order["TechnicianId"],
                       "EventType": rng.choice(EVENT_TYPES), "OccurredAt": _iso(occurred),
                       "Notes": rng.choice([None, "Replaced worn gasket.", "Recalibrated sensor.", "No fault found."])})
    return {"Locations": locations, "Technicians": technicians, "Equipment": equipment,
            "MaintenanceRequests": requests, "WorkOrders": work_orders, "ServiceEvents": events}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()
