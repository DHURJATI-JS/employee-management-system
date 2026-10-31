"""How complete a profile / a company is: the checklists behind the completion rings (Chart.js, app.js [data-completion]) on /profile and on a
company's dashboard (for its owner + co-owners: they're the ones who can finish it). Each item says why it matters and links to where it's
done. Only what makes a difference counts (being found, getting paid, being trusted): choices like being listed in search don't."""
from system import picture
def item(label: str, done, href: str, why: str) -> dict: return {"label": label, "done": bool(done), "href": href, "why": why}
def tally(groups: list[dict]) -> dict:
    """{"groups": [{label, rows, done, total}], done, total, left, pct, next (the first item still to do, or None), chart}. "rows", not
    "items": in Jinja g.items is the dict's own method"""
    for g in groups: g["done"], g["total"] = sum(i["done"] for i in g["rows"]), len(g["rows"])
    done, total = sum(g["done"] for g in groups), sum(g["total"] for g in groups)
    return {"groups": groups, "done": done, "total": total, "left": total - done, "pct": round(done * 100 / total) if total else 100,
            "next": next((i for g in groups for i in g["rows"] if not i["done"]), None), "chart": [{"label": g["label"], "done": g["done"]} for g in groups]}
def of_profile(user: dict, p, dob) -> dict:
    """user = request.state.user (picture, region, tz, identity, bank); p = their Profile (None until saved once); dob = their date of birth."""
    return tally([
        {"label": "about you", "rows": [item("a headline", p and p.headline.strip(), "#biodata", "the first line companies read"),
                                         item("a few lines about you", p and len(p.bio.strip()) >= 30, "#biodata", "what you've built and what you want next"),
                                         item("3 skills or more", p and len(p.skills or []) >= 3, "#biodata", "recommendations match on them"),
                                         item("years of experience", p and p.years is not None, "#biodata", "companies see it with your profile"),
                                         item("your CV", p and p.resume_file, "#cv", "its main points are read for matching")]},
        {"label": "account", "rows": [item("a profile picture", user.get("picture"), "/settings", "colleagues recognise you"),
                                       item("region and time zone", user.get("region") and user.get("tz"), "/settings#region", "your currency, holidays and clock"),
                                       item("date of birth", dob, "#biodata", "set once: companies see your age")]},
        {"label": "pay & identity", "rows": [item("identity proof", user.get("identity"), "/settings#identity", "everyone in a company needs one"),
                                              item("bank details", user.get("bank"), "/settings#bank", "where your salary goes"),
                                              item("the salary you want", p and p.want_min, "#salary", "companies that pay it are shown first")]}])
def of_company(c, cbank, people: int) -> dict:
    """c with salaries loaded; cbank = its CompanyBankAccount or None; people = how many are in it."""
    edit, card, pay = f"/workplaces#edit-{c.id}", f"/workplaces#c-{c.id}", c.salary_of()
    return tally([
        {"label": "profile", "rows": [item("a logo", picture("c", c.id), card, "it's seen better in search"),
                                       item("what it does", c.description.strip(), edit, "search and recommendations read it"),
                                       item("who it's hiring", c.hiring.strip(), edit, "people looking for work are matched with it"),
                                       item("a website", c.website, edit, "applicants check it")]},
        {"label": "verified", "rows": [item("registration proof", c.id_proof_at, "#finance", "salaries can't be paid until it's uploaded"),
                                        item("place on the map", c.location_place_id, edit, "checked, so applicants know it's real"),
                                        item("exact address", c.exact_address.strip(), edit, "the unit, floor and building")]},
        {"label": "money", "rows": [item("minimum salaries", c.salary_currency and all(pay.get(x) for x in c.positions), edit, "every position it offers needs one"),
                                     item("company account", cbank, "#finance", "salaries are paid from it"),
                                     item("funds", c.net_worth, "#finance", "nobody can join until they're above 0")]},
        {"label": "team", "rows": [item("someone besides the owner", people > 1, card, "share the join code"),
                                    item("its own terms", c.terms.strip(), "#terms", "on top of the standard ones")]}])
