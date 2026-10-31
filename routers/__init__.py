"""Every module in this folder that defines `router = APIRouter(...)` is included in the app automatically (see app.py).
Every endpoint requires a login unless it's decorated with @public (from decoraters), placed under the @router.<method> line.
Logged-in user: request.state.user -> {"user_id", "username", "companies"} (Redis session, re-checked against Postgres each request).
Pages: return render(request, "page", {"extra": ...} or extra=...) -> templates/page.html with app_name, user, path injected.
File inputs: give the form a `SavedFile` field (from decoraters). @as_form scans + saves it and flashes; the field holds the saved name.
Database: async SQLAlchemy only (db: AsyncSession = Depends(get_db)); relationships are lazy="raise", so load them with selectinload().
Routers can import:  decoraters (@public, @as_form, SavedFile, cache), form (RegisterForm/LoginForm/CompanyForm/JoinForm/..., use with
Depends(X.as_form)), tables (get_db, User, Company, Membership, POSITIONS, JOINABLE), system (render, APP_NAME, flash, back, wants_html,
FormError, UPLOAD_DIR, hash_password, verify_password, reddisdb)."""
