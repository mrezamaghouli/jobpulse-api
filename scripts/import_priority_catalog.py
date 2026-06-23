import re
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

from app.config import get_postgres_config


JOB_CATALOG = {
    "Software / Engineering / IT": [
        "Software Engineer", "Software Developer", "Backend Developer", "Backend Engineer",
        "Frontend Developer", "Frontend Engineer", "Full Stack Developer", "Full Stack Engineer",
        "Web Developer", "Mobile Developer", "Android Developer", "iOS Developer",
        "React Developer", "Vue Developer", "Angular Developer", "Node.js Developer",
        "Python Developer", "Java Developer", ".NET Developer", "PHP Developer",
        "Laravel Developer", "Django Developer", "Ruby on Rails Developer", "Go Developer",
        "Rust Developer", "C++ Developer", "C# Developer", "WordPress Developer",
        "Shopify Developer", "Game Developer", "Unity Developer", "Unreal Engine Developer",
        "Blockchain Developer", "Smart Contract Developer", "Web3 Developer", "Solidity Developer",
        "DevOps Engineer", "Cloud Engineer", "Cloud Architect", "AWS Engineer",
        "Azure Engineer", "Google Cloud Engineer", "Site Reliability Engineer", "SRE",
        "Platform Engineer", "Infrastructure Engineer", "Network Engineer", "Systems Engineer",
        "System Administrator", "Linux Administrator", "Database Administrator", "Data Engineer",
        "Machine Learning Engineer", "AI Engineer", "MLOps Engineer", "NLP Engineer",
        "Computer Vision Engineer", "Data Scientist", "Data Analyst", "BI Developer",
        "Business Intelligence Analyst", "Cybersecurity Analyst", "Security Engineer",
        "Application Security Engineer", "SOC Analyst", "Penetration Tester", "QA Engineer",
        "Software Test Engineer", "Automation QA Engineer", "Manual QA Tester",
        "Technical Support Engineer", "IT Support Specialist", "Help Desk Technician",
        "Solutions Architect", "Technical Architect", "Enterprise Architect",
    ],
    "Product / Project / Management": [
        "Product Manager", "Associate Product Manager", "Senior Product Manager",
        "Technical Product Manager", "Product Owner", "Product Lead", "Head of Product",
        "Chief Product Officer", "Project Manager", "Technical Project Manager",
        "Program Manager", "Engineering Manager", "Scrum Master", "Agile Coach",
        "Delivery Manager", "Operations Manager", "Business Operations Manager",
        "Strategy Manager", "Chief Operating Officer", "General Manager", "Team Lead",
        "Department Manager", "Office Manager",
    ],
    "Design / Creative / UI UX": [
        "UI Designer", "UX Designer", "UI/UX Designer", "Product Designer",
        "Visual Designer", "Graphic Designer", "Brand Designer", "Motion Designer",
        "Illustrator", "2D Artist", "3D Artist", "Game Artist", "Concept Artist",
        "Character Designer", "UX Researcher", "Interaction Designer", "Service Designer",
        "Web Designer", "Creative Director", "Art Director", "Design Lead", "Design Manager",
        "Brand Identity Designer", "Packaging Designer", "Presentation Designer",
    ],
    "Marketing / Growth / Content": [
        "Marketing Manager", "Digital Marketing Manager", "Performance Marketing Manager",
        "Growth Marketing Manager", "Growth Manager", "SEO Specialist", "SEO Manager",
        "SEM Specialist", "PPC Specialist", "Google Ads Specialist", "Social Media Manager",
        "Content Manager", "Content Strategist", "Copywriter", "Technical Writer",
        "Email Marketing Specialist", "CRM Marketing Manager", "Lifecycle Marketing Manager",
        "Affiliate Marketing Manager", "Influencer Marketing Manager", "Brand Manager",
        "Product Marketing Manager", "Marketing Analyst", "Marketing Operations Manager",
        "Community Manager", "Public Relations Manager", "Communications Manager",
    ],
    "Sales / Business Development / Customer": [
        "Sales Representative", "Sales Executive", "Account Executive", "Account Manager",
        "Key Account Manager", "Sales Manager", "Regional Sales Manager",
        "Inside Sales Representative", "Outside Sales Representative",
        "Business Development Representative", "BDR", "Sales Development Representative", "SDR",
        "Business Development Manager", "Partnerships Manager", "Channel Sales Manager",
        "Customer Success Manager", "Customer Success Specialist", "Customer Support Specialist",
        "Customer Service Representative", "Client Success Manager", "Client Relationship Manager",
        "Solutions Consultant", "Pre Sales Engineer", "Sales Engineer",
    ],
    "Finance / Accounting / Banking": [
        "Accountant", "Accounting Manager", "Financial Analyst", "Finance Manager",
        "Financial Controller", "Auditor", "Internal Auditor", "Tax Specialist", "Tax Manager",
        "Bookkeeper", "Payroll Specialist", "Investment Analyst", "Equity Analyst",
        "Risk Analyst", "Credit Analyst", "Banking Specialist", "Loan Officer",
        "Treasury Analyst", "Chief Financial Officer", "CFO", "FP&A Analyst",
        "Compliance Analyst",
    ],
    "HR / Recruiting": [
        "Human Resources Specialist", "HR Manager", "HR Business Partner",
        "Talent Acquisition Specialist", "Recruiter", "Technical Recruiter",
        "Recruitment Consultant", "People Operations Manager", "People Partner",
        "Learning and Development Specialist", "Compensation and Benefits Specialist",
        "Payroll Manager", "Employee Relations Specialist", "Chief People Officer",
    ],
    "Data / Analytics": [
        "Data Analyst", "Senior Data Analyst", "Business Analyst",
        "Business Intelligence Analyst", "BI Analyst", "BI Developer", "Data Engineer",
        "Analytics Engineer", "Data Scientist", "Machine Learning Engineer",
        "Research Analyst", "Quantitative Analyst", "Product Analyst", "Marketing Analyst",
        "Operations Analyst", "Financial Data Analyst", "Data Architect", "Database Developer",
    ],
    "Healthcare / Medical": [
        "Doctor", "Physician", "Nurse", "Registered Nurse", "Dentist", "Pharmacist",
        "Medical Assistant", "Healthcare Administrator", "Clinical Research Associate",
        "Clinical Data Manager", "Medical Sales Representative", "Physical Therapist",
        "Occupational Therapist", "Radiologist", "Lab Technician", "Biomedical Engineer",
        "Biostatistician",
    ],
    "Education / Research": [
        "Teacher", "English Teacher", "Instructor", "Lecturer", "Professor",
        "Research Assistant", "Researcher", "Research Scientist", "Academic Advisor",
        "Curriculum Developer", "Training Specialist", "Education Consultant",
        "Teaching Assistant", "PhD Researcher",
    ],
    "Legal / Compliance": [
        "Lawyer", "Attorney", "Legal Counsel", "Corporate Counsel", "Paralegal",
        "Legal Assistant", "Compliance Officer", "Compliance Manager", "Contract Manager",
        "Risk and Compliance Analyst", "Data Protection Officer",
    ],
    "Operations / Supply Chain / Logistics": [
        "Operations Specialist", "Operations Manager", "Supply Chain Manager",
        "Supply Chain Analyst", "Logistics Coordinator", "Logistics Manager",
        "Procurement Specialist", "Procurement Manager", "Purchasing Manager",
        "Inventory Manager", "Warehouse Manager", "Production Manager",
        "Manufacturing Engineer", "Process Engineer", "Quality Manager",
        "Quality Assurance Specialist", "Quality Control Inspector",
    ],
    "Engineering Non-Software": [
        "Mechanical Engineer", "Electrical Engineer", "Civil Engineer", "Structural Engineer",
        "Chemical Engineer", "Industrial Engineer", "Environmental Engineer", "Energy Engineer",
        "Biomedical Engineer", "Automotive Engineer", "Aerospace Engineer",
        "Manufacturing Engineer", "Process Engineer", "Maintenance Engineer",
        "Project Engineer", "Field Engineer", "Design Engineer", "R&D Engineer",
    ],
    "Executive / Founder": [
        "Founder", "Co Founder", "CEO", "Chief Executive Officer", "COO",
        "Chief Operating Officer", "CTO", "Chief Technology Officer", "CFO",
        "Chief Financial Officer", "CMO", "Chief Marketing Officer", "CPO",
        "Chief Product Officer", "Managing Director", "Director", "Vice President",
        "VP of Engineering", "VP of Sales", "VP of Marketing", "Head of Engineering",
        "Head of Growth", "Head of Sales", "Head of Operations",
    ],
}


COUNTRIES = [
    ("United States", 1),
    ("India", 1),
    ("Brazil", 1),
    ("United Kingdom", 1),
    ("Canada", 1),
    ("Germany", 1),
    ("France", 1),
    ("Netherlands", 1),
    ("Australia", 1),
    ("United Arab Emirates", 1),
    ("Singapore", 2),
    ("Ireland", 2),
    ("Spain", 2),
    ("Italy", 2),
    ("Sweden", 2),
    ("Switzerland", 2),
    ("Saudi Arabia", 2),
    ("Qatar", 2),
    ("Turkey", 2),
    ("Indonesia", 2),
    ("Mexico", 2),
    ("South Africa", 2),
    ("Poland", 2),
    ("Portugal", 2),
    ("Belgium", 2),
    ("Denmark", 2),
    ("Norway", 2),
    ("Finland", 2),
    ("New Zealand", 2),
    ("Malaysia", 2),
]


def normalize(value: str) -> str:
    value = value.strip().lower()
    value = value.replace("&", "and")
    value = re.sub(r"[^a-z0-9+#.]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def connect():
    return psycopg2.connect(**get_postgres_config())


def upsert_title(cursor, category: str, title: str):
    normalized_title = normalize(title)

    cursor.execute(
        """
        INSERT INTO job_catalog_titles (category, title, normalized_title, is_active, updated_at)
        VALUES (%s, %s, %s, TRUE, NOW())
        ON CONFLICT (normalized_title)
        DO UPDATE SET
            category = EXCLUDED.category,
            title = EXCLUDED.title,
            is_active = TRUE,
            updated_at = NOW()
        RETURNING id
        """,
        (category, title.strip(), normalized_title),
    )

    return cursor.fetchone()[0]


def upsert_country(cursor, country_name: str, priority: int):
    normalized_country = normalize(country_name)

    cursor.execute(
        """
        INSERT INTO job_catalog_countries (country_name, normalized_country, priority, is_active, updated_at)
        VALUES (%s, %s, %s, TRUE, NOW())
        ON CONFLICT (normalized_country)
        DO UPDATE SET
            country_name = EXCLUDED.country_name,
            priority = EXCLUDED.priority,
            is_active = TRUE,
            updated_at = NOW()
        RETURNING id
        """,
        (country_name.strip(), normalized_country, priority),
    )

    return cursor.fetchone()[0]


def upsert_coverage(cursor, title_id: int, country_id: int, title: str, country: str, priority: int):
    cursor.execute(
        """
        INSERT INTO job_collection_coverage (
            job_title_id,
            country_id,
            search_query,
            linkedin_location,
            country_priority,
            status,
            updated_at
        )
        VALUES (%s, %s, %s, %s, %s, 'pending', NOW())
        ON CONFLICT (job_title_id, country_id)
        DO UPDATE SET
            search_query = EXCLUDED.search_query,
            linkedin_location = EXCLUDED.linkedin_location,
            country_priority = EXCLUDED.country_priority,
            updated_at = NOW()
        """,
        (title_id, country_id, title.strip(), country.strip(), priority),
    )


def main():
    conn = connect()

    try:
        with conn.cursor() as cursor:
            title_ids = {}

            for category, titles in JOB_CATALOG.items():
                for title in titles:
                    normalized_title = normalize(title)
                    if normalized_title in title_ids:
                        continue
                    title_ids[normalized_title] = upsert_title(cursor, category, title)

            country_ids = {}
            country_priorities = {}

            for country, priority in COUNTRIES:
                normalized_country = normalize(country)
                country_ids[normalized_country] = upsert_country(cursor, country, priority)
                country_priorities[normalized_country] = priority

            for category, titles in JOB_CATALOG.items():
                for title in titles:
                    title_id = title_ids[normalize(title)]

                    for country, priority in COUNTRIES:
                        country_id = country_ids[normalize(country)]
                        upsert_coverage(cursor, title_id, country_id, title, country, priority)

        conn.commit()

        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SELECT COUNT(*) AS count FROM job_catalog_titles WHERE is_active = TRUE")
            titles_count = cursor.fetchone()["count"]

            cursor.execute("SELECT COUNT(*) AS count FROM job_catalog_countries WHERE is_active = TRUE")
            countries_count = cursor.fetchone()["count"]

            cursor.execute("SELECT COUNT(*) AS count FROM job_collection_coverage")
            coverage_count = cursor.fetchone()["count"]

            cursor.execute(
                """
                SELECT country_priority, COUNT(*) AS count
                FROM job_collection_coverage
                GROUP BY country_priority
                ORDER BY country_priority
                """
            )
            by_priority = cursor.fetchall()

        print("Priority catalog imported successfully.")
        print(f"Active job titles: {titles_count}")
        print(f"Active countries: {countries_count}")
        print(f"Coverage tasks: {coverage_count}")
        print("Coverage by priority:")
        for row in by_priority:
            print(f"  priority={row['country_priority']} count={row['count']}")

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


if __name__ == "__main__":
    main()
