#!/usr/bin/env python3
"""Generate synthetic HR datasets (1000 rows each) into this directory."""
from __future__ import annotations

import csv
import random
from datetime import date, datetime, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent
N = 1000
RNG = random.Random(7)

FIRST = [
    "Ava", "Liam", "Olivia", "Noah", "Emma", "Oliver", "Sophia", "Elijah", "Isabella", "Lucas",
    "Mia", "Mason", "Amelia", "Ethan", "Harper", "James", "Evelyn", "Benjamin", "Abigail", "Henry",
    "Emily", "Alexander", "Ella", "Michael", "Scarlett", "Daniel", "Grace", "Jacob", "Chloe", "Logan",
    "Zoey", "Jackson", "Lily", "Sebastian", "Aria", "Jack", "Riley", "Aiden", "Nora", "Owen",
    "Priya", "Arjun", "Sofia", "Mateo", "Ananya", "Wei", "Yuki", "Fatima", "Omar", "Elena",
]
LAST = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
    "Hernandez", "Lopez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee",
    "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
    "Patel", "Kim", "Chen", "Singh", "Khan", "Nguyen", "Ali", "Costa", "Silva", "Murphy",
]
DEPARTMENTS = [
    "Engineering", "Product", "Data", "Sales", "Marketing", "Finance", "HR", "Operations",
    "Customer Success", "Legal", "IT", "Security", "Supply Chain", "Facilities",
]
JOB_FAMILIES = [
    "Software Engineering", "Data Science", "Product Management", "Sales", "Marketing",
    "Finance & Accounting", "People Operations", "Customer Support", "IT Operations", "Legal",
]
LOCATIONS = [
    ("Austin", "TX", "US"), ("Dallas", "TX", "US"), ("New York", "NY", "US"),
    ("Chicago", "IL", "US"), ("Seattle", "WA", "US"), ("San Francisco", "CA", "US"),
    ("Denver", "CO", "US"), ("Atlanta", "GA", "US"), ("London", "ENG", "UK"),
    ("Bengaluru", "KA", "IN"), ("Hyderabad", "TS", "IN"), ("Toronto", "ON", "CA"),
]
EMP_STATUSES = ["active", "active", "active", "on_leave", "terminated", "retired"]
EMP_TYPES = ["full_time", "part_time", "contract", "intern"]
LEAVE_TYPES = ["annual", "sick", "personal", "parental", "bereavement", "unpaid", "comp_off"]
BENEFIT_PLANS = [
    ("MED-PPO", "Medical PPO", "health"),
    ("MED-HMO", "Medical HMO", "health"),
    ("DEN-BASIC", "Dental Basic", "dental"),
    ("DEN-PLUS", "Dental Plus", "dental"),
    ("VIS-STD", "Vision Standard", "vision"),
    ("LIFE-1X", "Life 1x Salary", "life"),
    ("LIFE-2X", "Life 2x Salary", "life"),
    ("401K-STD", "401(k) Standard", "retirement"),
    ("401K-PLUS", "401(k) Plus Match", "retirement"),
    ("WEL-GYM", "Wellness Gym", "wellness"),
]
TERM_REASONS = [
    "voluntary_resignation", "better_opportunity", "relocation", "performance",
    "restructuring", "end_of_contract", "retirement", "mutual_agreement", "attendance",
]
PROMO_REASONS = ["performance", "role_expansion", "market_adjustment", "critical_skill", "retention"]
HIKE_REASONS = ["annual_cycle", "promotion", "market_correction", "retention", "spot_adjustment"]
JR_STATUSES = ["draft", "open", "on_hold", "filled", "cancelled"]
TRANSFER_TYPES = ["department", "location", "manager", "entity", "temporary"]


def write_csv(name: str, fieldnames: list[str], rows: list[dict]) -> None:
    path = OUT / name
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {path.name}: {len(rows)} rows")


def money(lo: float, hi: float) -> str:
    return f"{RNG.uniform(lo, hi):.2f}"


def phone() -> str:
    return f"+1-{RNG.randint(200,999)}-{RNG.randint(200,999)}-{RNG.randint(1000,9999)}"


def email(first: str, last: str, i: int) -> str:
    return f"{first}.{last}{i}@corp.example.com".lower().replace(" ", "")


def rand_date(start: date, end: date) -> date:
    span = (end - start).days
    return start + timedelta(days=RNG.randint(0, max(span, 0)))


def grade_band(level: int) -> str:
    # G1..G12 style grades
    return f"G{level:02d}"


def salary_for_grade(level: int, location_country: str) -> float:
    base = 38000 + level * 11500 + RNG.uniform(-2000, 4000)
    if location_country == "US":
        base *= 1.15
    elif location_country == "UK":
        base *= 1.05
    elif location_country == "IN":
        base *= 0.35
    elif location_country == "CA":
        base *= 1.00
    return round(base, 2)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    today = date(2026, 8, 4)
    earliest = date(2014, 1, 1)

    # ---- grades (1000 synthetic grade/step combinations) ----
    grades = []
    for i in range(1, N + 1):
        level = ((i - 1) % 12) + 1
        step = ((i - 1) // 12) % 10 + 1
        grades.append(
            {
                "grade_id": f"GRD{i:04d}",
                "grade_code": grade_band(level),
                "grade_name": f"Grade {level} Step {step}",
                "grade_level": str(level),
                "step": str(step),
                "job_family": RNG.choice(JOB_FAMILIES),
                "min_salary": f"{salary_for_grade(level, 'US') * 0.85:.2f}",
                "mid_salary": f"{salary_for_grade(level, 'US'):.2f}",
                "max_salary": f"{salary_for_grade(level, 'US') * 1.25:.2f}",
                "currency": "USD",
                "is_executive": "true" if level >= 10 else "false",
                "effective_from": (earliest + timedelta(days=level * 30)).isoformat(),
            }
        )
    write_csv(
        "grades.csv",
        [
            "grade_id", "grade_code", "grade_name", "grade_level", "step", "job_family",
            "min_salary", "mid_salary", "max_salary", "currency", "is_executive", "effective_from",
        ],
        grades,
    )

    # ---- job_details (job catalog / positions) ----
    job_details = []
    titles_by_family = {
        "Software Engineering": ["Software Engineer", "Senior Software Engineer", "Staff Engineer", "Engineering Manager"],
        "Data Science": ["Data Analyst", "Data Scientist", "ML Engineer", "Analytics Manager"],
        "Product Management": ["Associate PM", "Product Manager", "Senior PM", "Group PM"],
        "Sales": ["SDR", "Account Executive", "Enterprise AE", "Sales Manager"],
        "Marketing": ["Marketing Specialist", "Content Manager", "Demand Gen Manager", "Marketing Director"],
        "Finance & Accounting": ["Accountant", "Financial Analyst", "Controller", "Finance Manager"],
        "People Operations": ["HR Generalist", "Recruiter", "HRBP", "People Manager"],
        "Customer Support": ["Support Agent", "Support Lead", "CS Manager", "Support Specialist"],
        "IT Operations": ["IT Support", "Systems Admin", "Cloud Engineer", "IT Manager"],
        "Legal": ["Legal Counsel", "Paralegal", "Compliance Analyst", "Privacy Counsel"],
    }
    for i in range(1, N + 1):
        family = RNG.choice(JOB_FAMILIES)
        title = RNG.choice(titles_by_family[family])
        level = RNG.randint(1, 12)
        city, region, country = RNG.choice(LOCATIONS)
        job_details.append(
            {
                "job_id": f"JOB{i:04d}",
                "job_code": f"{family[:3].upper()}-{level:02d}-{i:04d}",
                "job_title": title,
                "job_family": family,
                "department": RNG.choice(DEPARTMENTS),
                "grade_id": grades[((level - 1) * 80 + ((i - 1) % 80)) % N]["grade_id"],
                "grade_code": grade_band(level),
                "employment_type": RNG.choice(EMP_TYPES),
                "flsa_status": RNG.choice(["exempt", "non_exempt"]),
                "location_city": city,
                "location_region": region,
                "location_country": country,
                "is_people_manager": (
                    "true"
                    if ("Manager" in title or "Director" in title or (level >= 8 and RNG.random() > 0.5))
                    else "false"
                ),
                "headcount_budgeted": str(RNG.randint(1, 5)),
            }
        )
    write_csv(
        "job_details.csv",
        [
            "job_id", "job_code", "job_title", "job_family", "department", "grade_id",
            "grade_code", "employment_type", "flsa_status", "location_city",
            "location_region", "location_country", "is_people_manager", "headcount_budgeted",
        ],
        job_details,
    )

    # ---- employees ----
    employees = []
    for i in range(1, N + 1):
        first, last = RNG.choice(FIRST), RNG.choice(LAST)
        job = job_details[i - 1]
        hire_date = rand_date(earliest, date(2026, 6, 30))
        status = RNG.choice(EMP_STATUSES)
        term_date = ""
        if status == "terminated":
            term_date = rand_date(hire_date + timedelta(days=60), today).isoformat()
        level = int(job["grade_code"][1:])
        salary = salary_for_grade(level, job["location_country"])
        employees.append(
            {
                "employee_id": f"EMP{i:04d}",
                "employee_number": f"E{100000 + i}",
                "first_name": first,
                "last_name": last,
                "email": email(first, last, i),
                "phone": phone(),
                "gender": RNG.choice(["female", "male", "non_binary", "prefer_not_to_say"]),
                "date_of_birth": rand_date(date(1965, 1, 1), date(2004, 12, 31)).isoformat(),
                "job_id": job["job_id"],
                "department": job["department"],
                "grade_code": job["grade_code"],
                "manager_id": f"EMP{RNG.randint(1, N):04d}" if i > 10 else "",
                "location_city": job["location_city"],
                "location_country": job["location_country"],
                "employment_status": status,
                "employment_type": job["employment_type"],
                "hire_date": hire_date.isoformat(),
                "termination_date": term_date,
                "current_salary": f"{salary:.2f}",
                "currency": "USD" if job["location_country"] in {"US", "CA"} else ("GBP" if job["location_country"] == "UK" else "INR"),
            }
        )
    # avoid self-manager
    for emp in employees:
        if emp["manager_id"] == emp["employee_id"]:
            emp["manager_id"] = f"EMP{RNG.randint(1, N):04d}"
            if emp["manager_id"] == emp["employee_id"]:
                emp["manager_id"] = ""
    write_csv(
        "employees.csv",
        [
            "employee_id", "employee_number", "first_name", "last_name", "email", "phone",
            "gender", "date_of_birth", "job_id", "department", "grade_code", "manager_id",
            "location_city", "location_country", "employment_status", "employment_type",
            "hire_date", "termination_date", "current_salary", "currency",
        ],
        employees,
    )

    # ---- hires ----
    hires = []
    for i in range(1, N + 1):
        emp = employees[i - 1]
        job = job_details[i - 1]
        offer = date.fromisoformat(emp["hire_date"]) - timedelta(days=RNG.randint(7, 45))
        hires.append(
            {
                "hire_id": f"HIR{i:04d}",
                "employee_id": emp["employee_id"],
                "job_id": job["job_id"],
                "requisition_id": f"JR{i:04d}",
                "hire_date": emp["hire_date"],
                "offer_date": offer.isoformat(),
                "start_date": emp["hire_date"],
                "hire_source": RNG.choice(
                    ["careers_site", "referral", "linkedin", "agency", "campus", "internal"]
                ),
                "recruiter_employee_id": f"EMP{RNG.randint(1, min(50, N)):04d}",
                "starting_salary": emp["current_salary"],
                "bonus_eligible": "true" if RNG.random() > 0.35 else "false",
                "probation_end_date": (date.fromisoformat(emp["hire_date"]) + timedelta(days=90)).isoformat(),
                "hire_status": "completed" if emp["employment_status"] != "terminated" or RNG.random() > 0.2 else "completed",
            }
        )
    write_csv(
        "hires.csv",
        [
            "hire_id", "employee_id", "job_id", "requisition_id", "hire_date", "offer_date",
            "start_date", "hire_source", "recruiter_employee_id", "starting_salary",
            "bonus_eligible", "probation_end_date", "hire_status",
        ],
        hires,
    )

    # ---- job requisitions (JRs) ----
    jrs = []
    for i in range(1, N + 1):
        job = job_details[RNG.randrange(N)]
        opened = rand_date(date(2022, 1, 1), today)
        status = RNG.choice(JR_STATUSES)
        filled = ""
        if status == "filled":
            filled = rand_date(opened, today).isoformat()
        jrs.append(
            {
                "jr_id": f"JR{i:04d}",
                "jr_number": f"REQ-{2022000 + i}",
                "job_id": job["job_id"],
                "job_title": job["job_title"],
                "department": job["department"],
                "grade_code": job["grade_code"],
                "hiring_manager_id": f"EMP{RNG.randint(1, N):04d}",
                "recruiter_id": f"EMP{RNG.randint(1, min(80, N)):04d}",
                "headcount": str(RNG.randint(1, 3)),
                "jr_status": status,
                "priority": RNG.choice(["low", "medium", "high", "critical"]),
                "opened_date": opened.isoformat(),
                "target_fill_date": (opened + timedelta(days=RNG.randint(30, 120))).isoformat(),
                "filled_date": filled,
                "location_city": job["location_city"],
                "location_country": job["location_country"],
            }
        )
    write_csv(
        "job_requisitions.csv",
        [
            "jr_id", "jr_number", "job_id", "job_title", "department", "grade_code",
            "hiring_manager_id", "recruiter_id", "headcount", "jr_status", "priority",
            "opened_date", "target_fill_date", "filled_date", "location_city", "location_country",
        ],
        jrs,
    )

    # ---- terminations ----
    terminations = []
    for i in range(1, N + 1):
        emp = employees[i - 1]
        if emp["termination_date"]:
            term_date = date.fromisoformat(emp["termination_date"])
        else:
            # still create historical/synthetic termination records for volume (e.g. prior employment events)
            hire = date.fromisoformat(emp["hire_date"])
            term_date = rand_date(hire + timedelta(days=90), today)
        notice = term_date - timedelta(days=RNG.randint(0, 30))
        terminations.append(
            {
                "termination_id": f"TRM{i:04d}",
                "employee_id": emp["employee_id"],
                "job_id": emp["job_id"],
                "termination_date": term_date.isoformat(),
                "last_working_day": term_date.isoformat(),
                "notice_date": notice.isoformat(),
                "termination_type": RNG.choice(["voluntary", "involuntary", "retirement", "contract_end"]),
                "termination_reason": RNG.choice(TERM_REASONS),
                "rehire_eligible": "true" if RNG.random() > 0.4 else "false",
                "exit_interview_completed": "true" if RNG.random() > 0.45 else "false",
                "initiated_by": RNG.choice(["employee", "manager", "hr", "system"]),
                "department": emp["department"],
            }
        )
    write_csv(
        "terminations.csv",
        [
            "termination_id", "employee_id", "job_id", "termination_date", "last_working_day",
            "notice_date", "termination_type", "termination_reason", "rehire_eligible",
            "exit_interview_completed", "initiated_by", "department",
        ],
        terminations,
    )

    # ---- transfers ----
    transfers = []
    for i in range(1, N + 1):
        emp = employees[RNG.randrange(N)]
        from_job = job_details[RNG.randrange(N)]
        to_job = job_details[RNG.randrange(N)]
        eff = rand_date(date.fromisoformat(emp["hire_date"]), today)
        transfers.append(
            {
                "transfer_id": f"XFR{i:04d}",
                "employee_id": emp["employee_id"],
                "transfer_type": RNG.choice(TRANSFER_TYPES),
                "from_job_id": from_job["job_id"],
                "to_job_id": to_job["job_id"],
                "from_department": from_job["department"],
                "to_department": to_job["department"],
                "from_location": f"{from_job['location_city']}, {from_job['location_country']}",
                "to_location": f"{to_job['location_city']}, {to_job['location_country']}",
                "from_manager_id": f"EMP{RNG.randint(1, N):04d}",
                "to_manager_id": f"EMP{RNG.randint(1, N):04d}",
                "effective_date": eff.isoformat(),
                "reason": RNG.choice(
                    ["org_redesign", "career_growth", "business_need", "employee_request", "backfill"]
                ),
                "status": RNG.choice(["approved", "approved", "pending", "completed", "cancelled"]),
            }
        )
    write_csv(
        "transfers.csv",
        [
            "transfer_id", "employee_id", "transfer_type", "from_job_id", "to_job_id",
            "from_department", "to_department", "from_location", "to_location",
            "from_manager_id", "to_manager_id", "effective_date", "reason", "status",
        ],
        transfers,
    )

    # ---- promotions ----
    promotions = []
    for i in range(1, N + 1):
        emp = employees[RNG.randrange(N)]
        old_level = max(1, int(emp["grade_code"][1:]) - RNG.randint(0, 2))
        new_level = min(12, old_level + RNG.randint(1, 2))
        eff = rand_date(date.fromisoformat(emp["hire_date"]) + timedelta(days=180), today)
        old_sal = float(emp["current_salary"]) * RNG.uniform(0.75, 0.95)
        new_sal = old_sal * RNG.uniform(1.06, 1.22)
        promotions.append(
            {
                "promotion_id": f"PRM{i:04d}",
                "employee_id": emp["employee_id"],
                "from_job_id": job_details[RNG.randrange(N)]["job_id"],
                "to_job_id": job_details[RNG.randrange(N)]["job_id"],
                "from_grade_code": grade_band(old_level),
                "to_grade_code": grade_band(new_level),
                "from_salary": f"{old_sal:.2f}",
                "to_salary": f"{new_sal:.2f}",
                "salary_increase_pct": f"{((new_sal - old_sal) / old_sal) * 100:.2f}",
                "effective_date": eff.isoformat(),
                "promotion_reason": RNG.choice(PROMO_REASONS),
                "approved_by": f"EMP{RNG.randint(1, N):04d}",
                "status": RNG.choice(["approved", "completed", "completed", "rescinded"]),
            }
        )
    write_csv(
        "promotions.csv",
        [
            "promotion_id", "employee_id", "from_job_id", "to_job_id", "from_grade_code",
            "to_grade_code", "from_salary", "to_salary", "salary_increase_pct",
            "effective_date", "promotion_reason", "approved_by", "status",
        ],
        promotions,
    )

    # ---- salary_hikes ----
    salary_hikes = []
    for i in range(1, N + 1):
        emp = employees[RNG.randrange(N)]
        eff = rand_date(date.fromisoformat(emp["hire_date"]) + timedelta(days=120), today)
        old_sal = float(emp["current_salary"]) * RNG.uniform(0.85, 1.0)
        pct = RNG.choice([2.0, 3.0, 3.5, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0])
        if RNG.random() > 0.85:
            pct = RNG.uniform(1.0, 15.0)
        new_sal = old_sal * (1 + pct / 100)
        salary_hikes.append(
            {
                "salary_hike_id": f"SLH{i:04d}",
                "employee_id": emp["employee_id"],
                "effective_date": eff.isoformat(),
                "previous_salary": f"{old_sal:.2f}",
                "new_salary": f"{new_sal:.2f}",
                "increase_amount": f"{(new_sal - old_sal):.2f}",
                "increase_pct": f"{pct:.2f}",
                "currency": emp["currency"],
                "hike_reason": RNG.choice(HIKE_REASONS),
                "review_cycle": RNG.choice(["H1", "H2", "Annual", "Off-cycle"]),
                "approved_by": f"EMP{RNG.randint(1, N):04d}",
                "status": RNG.choice(["approved", "paid", "pending", "rejected"]),
            }
        )
    write_csv(
        "salary_hikes.csv",
        [
            "salary_hike_id", "employee_id", "effective_date", "previous_salary", "new_salary",
            "increase_amount", "increase_pct", "currency", "hike_reason", "review_cycle",
            "approved_by", "status",
        ],
        salary_hikes,
    )

    # ---- benefits ----
    benefits = []
    for i in range(1, N + 1):
        emp = employees[RNG.randrange(N)]
        plan_code, plan_name, plan_type = RNG.choice(BENEFIT_PLANS)
        enroll = rand_date(date.fromisoformat(emp["hire_date"]), today)
        benefits.append(
            {
                "benefit_id": f"BEN{i:04d}",
                "employee_id": emp["employee_id"],
                "plan_code": plan_code,
                "plan_name": plan_name,
                "plan_type": plan_type,
                "coverage_level": RNG.choice(["employee", "employee_spouse", "employee_children", "family"]),
                "employer_contribution": money(50, 650),
                "employee_contribution": money(0, 350),
                "currency": emp["currency"],
                "enrollment_date": enroll.isoformat(),
                "effective_date": enroll.isoformat(),
                "end_date": ""
                if RNG.random() > 0.15
                else rand_date(enroll, today).isoformat(),
                "status": RNG.choice(["active", "active", "waived", "terminated"]),
            }
        )
    write_csv(
        "benefits.csv",
        [
            "benefit_id", "employee_id", "plan_code", "plan_name", "plan_type",
            "coverage_level", "employer_contribution", "employee_contribution", "currency",
            "enrollment_date", "effective_date", "end_date", "status",
        ],
        benefits,
    )

    # ---- leaves ----
    leaves = []
    for i in range(1, N + 1):
        emp = employees[RNG.randrange(N)]
        leave_type = RNG.choice(LEAVE_TYPES)
        start_d = rand_date(date.fromisoformat(emp["hire_date"]), today)
        days = RNG.randint(1, 14 if leave_type != "parental" else 90)
        end_d = start_d + timedelta(days=max(days - 1, 0))
        leaves.append(
            {
                "leave_id": f"LVE{i:04d}",
                "employee_id": emp["employee_id"],
                "leave_type": leave_type,
                "start_date": start_d.isoformat(),
                "end_date": end_d.isoformat(),
                "days_requested": str(days),
                "days_approved": str(days if RNG.random() > 0.1 else max(days - 1, 0)),
                "status": RNG.choice(["approved", "approved", "pending", "rejected", "cancelled"]),
                "approver_id": emp["manager_id"] or f"EMP{RNG.randint(1, N):04d}",
                "reason": RNG.choice(
                    ["vacation", "illness", "family", "personal matter", "medical", "travel", ""]
                ),
                "paid_flag": "false" if leave_type == "unpaid" else "true",
                "submitted_at": datetime.combine(start_d - timedelta(days=RNG.randint(1, 20)), datetime.min.time()).isoformat(sep=" "),
            }
        )
    write_csv(
        "leaves.csv",
        [
            "leave_id", "employee_id", "leave_type", "start_date", "end_date",
            "days_requested", "days_approved", "status", "approver_id", "reason",
            "paid_flag", "submitted_at",
        ],
        leaves,
    )

    print(f"\nDone. Files written under {OUT}")


if __name__ == "__main__":
    main()
