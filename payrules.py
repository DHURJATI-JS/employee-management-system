"""Payroll rules per salary currency (its country): what an employer withholds and pays on a salary, shown when the currency is picked
(company form) and in the pay dialog. AI-written from general knowledge (last reviewed October 2026): a summary that can be wrong or out
of date and is never legal or tax advice. Every block says so and links the official source. tax = the name of the money withheld from
the salary (the pay dialog's second box and the payslip use it); no_tax = no personal income tax there, so nothing should be withheld."""
from system import templates
AI_WARNING = "AI-written summary: it can contain mistakes or be out of date, and it isn't legal or tax advice. Check the official source (or an accountant) before paying anyone."
RULES = {
    "INR": {"country": "India", "tax": "TDS", "points": [
        "Income tax is deducted at source (TDS, section 192 of the Income-tax Act) at the employee's average rate for the year, under the tax regime they chose, and deposited by the 7th of the next month; Form 16 is issued after the year.",
        "Provident fund (EPF): 12% of basic wages from the employee plus 12% from the employer, for establishments with 20 or more employees.",
        "ESI: 0.75% from the employee and 3.25% from the employer, for wages up to ₹21,000 a month where ESI applies; some states also levy professional tax.",
        "Wages are due within 7 days after the wage period (10 days for 1,000+ workers) and should be paid by bank transfer."],
        "source": ("Income Tax Department", "https://www.incometax.gov.in/")},
    "USD": {"country": "United States", "tax": "tax withheld", "points": [
        "Federal income tax is withheld from each payment using the employee's Form W-4 (IRS Publication 15-T); most states, and some cities, withhold income tax too.",
        "FICA: Social Security 6.2% from the employee and 6.2% from the employer up to the year's wage base, and Medicare 1.45% each (plus 0.9% from the employee above $200,000).",
        "The employer alone pays federal (FUTA) and state unemployment tax; deposits follow the IRS schedule, and a W-2 goes to each employee after the year."],
        "source": ("IRS: employment taxes", "https://www.irs.gov/businesses/small-businesses-self-employed/employment-taxes")},
    "EUR": {"country": "the euro area", "tax": "tax + social security withheld", "points": [
        "There's no single EU payroll: the country where the work is done sets the income tax withheld from salaries (e.g. Lohnsteuer in Germany, prélèvement à la source in France) and the social security that both employee and employer pay.",
        "Rates, thresholds and payslip rules differ by country; the employer usually has to register there and report every payment to its tax and social security offices.",
        "Someone working in more than one EU country is covered by one country's social security only (an A1 certificate shows which)."],
        "source": ("Your Europe: tax and social security", "https://europa.eu/youreurope/citizens/work/")},
    "GBP": {"country": "the United Kingdom", "tax": "PAYE + NI", "points": [
        "PAYE: income tax is taken from each payment using the employee's tax code, together with employee National Insurance; the employer pays its own National Insurance on top above the threshold.",
        "Every payment is reported to HMRC in real time (a Full Payment Submission) on or before payday, and each employee gets a payslip.",
        "Eligible staff are auto-enrolled into a workplace pension: at least 8% of qualifying earnings in total, at least 3% from the employer."],
        "source": ("GOV.UK: running payroll", "https://www.gov.uk/running-payroll")},
    "AUD": {"country": "Australia", "tax": "PAYG withheld", "points": [
        "PAYG withholding: tax is withheld from each payment using the ATO's tax tables and the employee's TFN declaration.",
        "Superannuation guarantee: the employer pays super on top of wages (12% of ordinary time earnings from 1 July 2025), to the employee's fund.",
        "Each payment is reported to the ATO through Single Touch Payroll on or before payday, and a payslip is due within one working day."],
        "source": ("ATO: employers", "https://www.ato.gov.au/businesses-and-organisations/hiring-and-paying-your-workers")},
    "CAD": {"country": "Canada", "tax": "source deductions", "points": [
        "Federal and provincial income tax is withheld from each payment (based on the TD1 forms), plus CPP contributions (matched by the employer) and EI premiums (the employer pays 1.4 times the employee's).",
        "Quebec runs its own pension plan (QPP) and parental insurance instead; the deductions are remitted to the CRA (and Revenu Québec) on the due dates.",
        "A T4 slip goes to each employee after the year."],
        "source": ("Canada Revenue Agency: payroll", "https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll.html")},
    "SGD": {"country": "Singapore", "tax": "deductions (CPF)", "points": [
        "Income tax isn't withheld from residents' salaries: employees file their own; the employer reports earnings to IRAS (Form IR8A, through AIS where required).",
        "CPF for citizens and permanent residents: up to 20% from the employee and 17% from the employer (age 55 and below; lower rates above that), up to the monthly ceiling.",
        "Salary is due within 7 days after the salary period, with an itemised payslip."],
        "source": ("Ministry of Manpower: salary", "https://www.mom.gov.sg/employment-practices/salary")},
    "AED": {"country": "the United Arab Emirates", "tax": "deductions", "no_tax": True, "points": [
        "There's no personal income tax: nothing is withheld from salaries for tax, so the deductions box should normally be 0.",
        "Salaries of most private-sector staff must be paid through the Wage Protection System (WPS), via an approved bank or exchange house, on time.",
        "UAE and GCC nationals contribute to the national pension (GPSSA); others build up an end-of-service gratuity instead."],
        "source": ("MOHRE: wages", "https://www.mohre.gov.ae/")},
}
def tax_label(currency: str) -> str: return RULES.get(currency, {}).get("tax", "tax withheld")
templates.env.globals.update(pay_rules=RULES, ai_warning=AI_WARNING, tax_label=tax_label)  # {{ pay_rules['INR'] }}, {{ tax_label(c.salary_currency) }}
