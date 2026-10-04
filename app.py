
import os, sqlite3, math, urllib.request, json, secrets
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, g
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "lifelink.db")
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("LIFELINK_SECRET", "lifelink-dev-secret-change-me")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

BLOOD_GROUPS = ["A+","A-","B+","B-","AB+","AB-","O+","O-"]
ROLES = ["donor","recipient","hospital","camp_organizer","admin"]
URGENCY = ["Critical","Urgent","Routine"]
REQUEST_STATES = ["Active","Notified","Committed","Confirmed","Donated","Cancelled","Expired"]
COOLDOWN_DAYS = int(os.environ.get("LIFELINK_COOLDOWN_DAYS", "90"))
RADIUS = {"Critical": 15, "Urgent": 10, "Routine": 5}
MAX_RADIUS = 50
EXPANSION_MINUTES = int(os.environ.get("LIFELINK_EXPANSION_MINUTES", "30"))

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(exc):
    c=g.pop("db",None)
    if c: c.close()

def q(sql, args=(), one=False):
    cur=db().execute(sql,args)
    rows=cur.fetchone() if one else cur.fetchall()
    cur.close()
    return rows

def execute(sql,args=()):
    cur=db().execute(sql,args); db().commit(); return cur.lastrowid

def now():
    return datetime.utcnow().replace(microsecond=0).isoformat()

def init_db():
    con=sqlite3.connect(DB_PATH)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL, role TEXT NOT NULL, phone TEXT, city TEXT,
      lat REAL, lng REAL, blood_group TEXT, last_donation TEXT,
      verified INTEGER DEFAULT 1, active INTEGER DEFAULT 1, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS blood_requests(
      id INTEGER PRIMARY KEY AUTOINCREMENT, requester_id INTEGER NOT NULL, patient_name TEXT NOT NULL,
      blood_group TEXT NOT NULL, units INTEGER NOT NULL, urgency TEXT NOT NULL, location TEXT NOT NULL,
      lat REAL, lng REAL, contact TEXT NOT NULL, notes TEXT, status TEXT NOT NULL DEFAULT 'Active',
      radius_km REAL NOT NULL DEFAULT 5, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(requester_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS invitations(
      id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL, donor_id INTEGER NOT NULL,
      distance_km REAL, travel_minutes REAL, status TEXT NOT NULL DEFAULT 'Not Responded',
      created_at TEXT NOT NULL, responded_at TEXT, UNIQUE(request_id,donor_id),
      FOREIGN KEY(request_id) REFERENCES blood_requests(id) ON DELETE CASCADE,
      FOREIGN KEY(donor_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS donations(
      id INTEGER PRIMARY KEY AUTOINCREMENT, donor_id INTEGER NOT NULL, request_id INTEGER,
      donation_type TEXT NOT NULL, donation_date TEXT NOT NULL, units INTEGER DEFAULT 1, camp_id INTEGER,
      verifying_org_id INTEGER NOT NULL, verified_at TEXT NOT NULL,
      FOREIGN KEY(donor_id) REFERENCES users(id), FOREIGN KEY(request_id) REFERENCES blood_requests(id), FOREIGN KEY(camp_id) REFERENCES camps(id),
      FOREIGN KEY(verifying_org_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS organ_pledges(
      id INTEGER PRIMARY KEY AUTOINCREMENT, donor_id INTEGER NOT NULL, organ TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'Active', emergency_contact TEXT, consent INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(donor_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS organ_connections(
      id INTEGER PRIMARY KEY AUTOINCREMENT, pledge_id INTEGER NOT NULL, hospital_id INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'Connected', created_at TEXT NOT NULL,
      UNIQUE(pledge_id,hospital_id), FOREIGN KEY(pledge_id) REFERENCES organ_pledges(id) ON DELETE CASCADE,
      FOREIGN KEY(hospital_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS camps(
      id INTEGER PRIMARY KEY AUTOINCREMENT, organizer_id INTEGER NOT NULL, name TEXT NOT NULL,
      location TEXT NOT NULL, event_date TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(organizer_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS camp_targets(
      id INTEGER PRIMARY KEY AUTOINCREMENT, camp_id INTEGER NOT NULL, blood_group TEXT NOT NULL,
      target_units INTEGER NOT NULL, collected_units INTEGER DEFAULT 0,
      UNIQUE(camp_id,blood_group), FOREIGN KEY(camp_id) REFERENCES camps(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS activity(
      id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, action TEXT NOT NULL,
      details TEXT, created_at TEXT NOT NULL, FOREIGN KEY(actor_id) REFERENCES users(id)
    );
    """)
    con.commit()
    # seed demo accounts
    existing=con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if existing==0:
        seed=[
          ("Demo Donor","donor@lifelink.local","donor","9999999991","Indore",22.7196,75.8577,"O+",
           (datetime.utcnow()-timedelta(days=150)).date().isoformat(),1),
          ("Demo Recipient","recipient@lifelink.local","recipient","9999999992","Indore",22.7196,75.8577,None,None,1),
          ("City Hospital","hospital@lifelink.local","hospital","9999999993","Indore",22.7248,75.8654,None,None,1),
          ("Camp Organizer","camp@lifelink.local","camp_organizer","9999999994","Indore",22.72,75.86,None,None,1),
          ("System Admin","admin@lifelink.local","admin","9999999995","Indore",22.72,75.86,None,None,1)
        ]
        for name,email,role,phone,city,lat,lng,bg,ld,verified in seed:
            con.execute("""INSERT INTO users(name,email,password_hash,role,phone,city,lat,lng,blood_group,last_donation,verified,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (name,email,generate_password_hash("LifeLink@123", method="pbkdf2:sha256"),role,phone,city,lat,lng,bg,ld,verified,datetime.utcnow().isoformat()))
        con.commit()
    con.close()

def log(actor, action, details=""):
    execute("INSERT INTO activity(actor_id,action,details,created_at) VALUES(?,?,?,?)",
            (actor,action,details,now()))

def current_user():
    uid=session.get("user_id")
    return q("SELECT * FROM users WHERE id=? AND active=1",(uid,),one=True) if uid else None

@app.context_processor
def inject():
    return {"current_user": current_user(), "blood_groups": BLOOD_GROUPS, "cooldown": COOLDOWN_DAYS}

def login_required(f):
    @wraps(f)
    def wrap(*a,**kw):
        if not current_user(): return redirect(url_for("login", next=request.path))
        return f(*a,**kw)
    return wrap

def role_required(*roles):
    def deco(f):
        @wraps(f)
        def wrap(*a,**kw):
            u=current_user()
            if not u: return redirect(url_for("login",next=request.path))
            if u["role"] not in roles:
                flash("You do not have permission to access this page.","error")
                return redirect(url_for("dashboard"))
            return f(*a,**kw)
        return wrap
    return deco

def compat(donor, needed):
    matrix={
      "O-": set(BLOOD_GROUPS), "O+": {"O+","A+","B+","AB+"},
      "A-":{"A-","A+","AB-","AB+"},"A+": {"A+","AB+"},
      "B-":{"B-","B+","AB-","AB+"},"B+": {"B+","AB+"},
      "AB-":{"AB-","AB+"},"AB+": {"AB+"}
    }
    return needed in matrix.get(donor,set())

def eligible(donor):
    if not donor["blood_group"]: return False
    if not donor["last_donation"]: return True
    try: last=datetime.fromisoformat(donor["last_donation"])
    except: return False
    return datetime.utcnow()-last >= timedelta(days=COOLDOWN_DAYS)

def haversine(a,b,c,d):
    R=6371.0
    p1,p2=math.radians(a),math.radians(c)
    dp=math.radians(c-a); dl=math.radians(d-b)
    x=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(x))

def osrm_distance(lat1,lng1,lat2,lng2):
    try:
        url=f"https://router.project-osrm.org/route/v1/driving/{lng1},{lat1};{lng2},{lat2}?overview=false"
        req=urllib.request.Request(url,headers={"User-Agent":"LifeLink/1.0"})
        with urllib.request.urlopen(req,timeout=4) as r:
            data=json.loads(r.read().decode())
        route=data["routes"][0]
        return route["distance"]/1000, route["duration"]/60
    except Exception:
        return haversine(lat1,lng1,lat2,lng2), None

def current_radius(req):
    base=RADIUS[req["urgency"]]
    elapsed=(datetime.utcnow()-datetime.fromisoformat(req["created_at"])).total_seconds()/60
    expansions=max(0,int(elapsed//EXPANSION_MINUTES))
    return min(MAX_RADIUS,base*(2**expansions))

def match_request(request_id):
    req=q("SELECT * FROM blood_requests WHERE id=?",(request_id,),one=True)
    if not req or req["status"] in ("Cancelled","Expired","Donated"): return []
    radius=current_radius(req)
    if radius != req["radius_km"]:
        execute("UPDATE blood_requests SET radius_km=?,updated_at=? WHERE id=?",(radius,now(),request_id))
    committed=q("""SELECT COALESCE(SUM(CASE WHEN status IN ('Accepted','Confirmed') THEN 1 ELSE 0 END),0)
                   FROM invitations WHERE request_id=?""",(request_id,),one=True)[0]
    needed=req["units"]
    if committed >= needed: return []
    donors=q("""SELECT * FROM users WHERE role='donor' AND active=1 AND verified=1
                AND blood_group IS NOT NULL AND lat IS NOT NULL AND lng IS NOT NULL""")
    out=[]
    for d in donors:
        if not compat(d["blood_group"],req["blood_group"]) or not eligible(d): continue
        if q("SELECT 1 FROM invitations WHERE request_id=? AND donor_id=?",(request_id,d["id"]),one=True): continue
        if req["lat"] is None or req["lng"] is None: continue
        dist,mins=osrm_distance(req["lat"],req["lng"],d["lat"],d["lng"])
        if dist <= radius:
            out.append((dist if dist is not None else 999999,mins,d["id"]))
    out.sort(key=lambda x:x[0])
    # notify only enough candidates to fill outstanding units, plus one backup
    for dist,mins,did in out[:max(needed-int(committed),1)]:
        execute("""INSERT OR IGNORE INTO invitations(request_id,donor_id,distance_km,travel_minutes,status,created_at)
                   VALUES(?,?,?,?,?,?)""",(request_id,did,dist,mins,"Not Responded",now()))
    if out and req["status"]=="Active":
        execute("UPDATE blood_requests SET status='Notified',updated_at=? WHERE id=?",(now(),request_id))
    return out

def serialize(row):
    return dict(row) if row else None

@app.route("/")
def index(): return render_template("index.html")

@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="POST":
        email=request.form.get("email","").strip().lower()
        password=request.form.get("password","")
        u=q("SELECT * FROM users WHERE lower(email)=? AND active=1",(email,),one=True)
        if u and check_password_hash(u["password_hash"],password):
            session.clear(); session["user_id"]=u["id"]
            log(u["id"],"LOGIN","Successful login")
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.","error")
    return render_template("login.html")

@app.route("/register",methods=["GET","POST"])
def register():
    if request.method=="POST":
        data={k:request.form.get(k,"").strip() for k in ["name","email","phone","city","role","blood_group","last_donation"]}
        pw=request.form.get("password",""); cp=request.form.get("confirm_password","")
        if not data["name"] or not data["email"] or not pw or pw!=cp:
            flash("Please complete all required fields and ensure passwords match.","error")
        elif data["role"] not in ROLES or data["role"]=="admin":
            flash("Select a valid user role.","error")
        elif data["role"]=="donor" and data["blood_group"] not in BLOOD_GROUPS:
            flash("Donors must select a blood group.","error")
        else:
            try:
                verified=1 if data["role"] not in ("hospital",) else 0
                uid=execute("""INSERT INTO users(name,email,password_hash,role,phone,city,blood_group,last_donation,verified,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",(data["name"],data["email"],generate_password_hash(pw, method="pbkdf2:sha256"),data["role"],
                    data["phone"],data["city"],data["blood_group"] or None,data["last_donation"] or None,verified,now()))
                log(uid,"REGISTER",f"Role: {data['role']}")
                flash("Account created. Hospital accounts require admin verification before creating requests.","success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError: flash("An account with that email already exists.","error")
    return render_template("register.html")

@app.route("/logout")
def logout():
    uid=session.get("user_id")
    if uid: log(uid,"LOGOUT")
    session.clear(); return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    role=current_user()["role"]
    return redirect(url_for({"donor":"donor_dashboard","recipient":"needy_dashboard","hospital":"hospital_dashboard",
                     "admin":"admin_dashboard","camp_organizer":"camp_dashboard"}[role]))

@app.route("/donor-dashboard")
@role_required("donor")
def donor_dashboard():
    u=current_user()
    inv=q("SELECT COUNT(*) n FROM invitations WHERE donor_id=? AND status='Not Responded'",(u["id"],),one=True)["n"]
    acc=q("SELECT COUNT(*) n FROM invitations WHERE donor_id=? AND status IN ('Accepted','Confirmed')",(u["id"],),one=True)["n"]
    hist=q("SELECT COUNT(*) n FROM donations WHERE donor_id=?",(u["id"],),one=True)["n"]
    pled=q("SELECT COUNT(*) n FROM organ_pledges WHERE donor_id=? AND status='Active'",(u["id"],),one=True)["n"]
    return render_template("donor-dashboard.html",stats=(inv,acc,hist,pled))

@app.route("/profile",methods=["GET","POST"])
@role_required("donor","recipient","camp_organizer")
def profile():
    u=current_user()
    if request.method=="POST":
        bg=request.form.get("blood_group") or None
        if u["role"]=="donor" and bg not in BLOOD_GROUPS: flash("Select a valid blood group.","error")
        else:
            execute("""UPDATE users SET name=?,phone=?,city=?,lat=?,lng=?,blood_group=?,last_donation=? WHERE id=?""",
                    (request.form.get("name","").strip(),request.form.get("phone","").strip(),
                     request.form.get("city","").strip(),float(request.form["lat"]) if request.form.get("lat") else None,
                     float(request.form["lng"]) if request.form.get("lng") else None,bg,
                     request.form.get("last_donation") or None,u["id"]))
            log(u["id"],"PROFILE_UPDATED")
            flash("Profile saved.","success")
        return redirect(url_for("profile"))
    return render_template("profile.html",u=u)

@app.route("/blood-requests")
@role_required("donor")
def blood_requests():
    u=current_user()
    reqs=q("""SELECT br.*,u.name requester FROM blood_requests br JOIN users u ON u.id=br.requester_id
              WHERE br.status NOT IN ('Cancelled','Expired','Donated') ORDER BY
              CASE urgency WHEN 'Critical' THEN 1 WHEN 'Urgent' THEN 2 ELSE 3 END, created_at DESC""")
    # Run matching so eligible new invitations appear in database
    for r in reqs: match_request(r["id"])
    inv=q("""SELECT i.*,br.patient_name,br.blood_group,br.units,br.urgency,br.location,u.name requester
             FROM invitations i JOIN blood_requests br ON br.id=i.request_id JOIN users u ON u.id=br.requester_id
             WHERE i.donor_id=? ORDER BY i.created_at DESC""",(u["id"],))
    return render_template("blood-requests.html",requests=reqs,invitations=inv)

@app.route("/needy-dashboard")
@role_required("recipient")
def needy_dashboard():
    u=current_user()
    stats=(
      q("SELECT COUNT(*) n FROM blood_requests WHERE requester_id=? AND status NOT IN ('Cancelled','Donated','Expired')",(u["id"],),one=True)["n"],
      q("SELECT COUNT(DISTINCT i.donor_id) n FROM invitations i JOIN blood_requests r ON r.id=i.request_id WHERE r.requester_id=?",(u["id"],),one=True)["n"],
      q("SELECT COUNT(*) n FROM invitations i JOIN blood_requests r ON r.id=i.request_id WHERE r.requester_id=? AND i.status IN ('Accepted','Confirmed')",(u["id"],),one=True)["n"]
    )
    return render_template("needy-dashboard.html",stats=stats)

@app.route("/create-request",methods=["GET","POST"])
@role_required("recipient","hospital")
def create_request():
    u=current_user()
    if u["role"]=="hospital" and not u["verified"]:
        flash("Your hospital account must be verified by an admin first.","error")
        return redirect(url_for("hospital_dashboard"))
    if request.method=="POST":
        try: units=max(1,int(request.form.get("units","1")))
        except: units=1
        bg=request.form.get("blood_group")
        urg=request.form.get("urgency")
        lat=float(request.form["lat"]) if request.form.get("lat") else u["lat"]
        lng=float(request.form["lng"]) if request.form.get("lng") else u["lng"]
        if bg not in BLOOD_GROUPS or urg not in URGENCY or not request.form.get("patient_name") or not request.form.get("location") or not request.form.get("contact"):
            flash("Please complete all required request fields.","error")
        else:
            rid=execute("""INSERT INTO blood_requests(requester_id,patient_name,blood_group,units,urgency,location,lat,lng,contact,notes,status,radius_km,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(u["id"],request.form["patient_name"].strip(),bg,units,urg,
                request.form["location"].strip(),lat,lng,request.form["contact"].strip(),request.form.get("notes","").strip(),
                "Active",RADIUS[urg],now(),now()))
            log(u["id"],"REQUEST_CREATED",f"Request #{rid}")
            match_request(rid)
            flash(f"Blood request #{rid} created and matching started.","success")
            return redirect(url_for("request_status",request_id=rid))
    return render_template("create-request.html",u=u)

@app.route("/request-status")
@role_required("recipient")
def request_status_default():
    r=q("SELECT id FROM blood_requests WHERE requester_id=? ORDER BY created_at DESC LIMIT 1",(current_user()["id"],),one=True)
    return redirect(url_for("request_status",request_id=r["id"])) if r else redirect(url_for("create_request"))

@app.route("/request-status/<int:request_id>")
@login_required
def request_status(request_id):
    u=current_user()
    r=q("SELECT br.*,u.name requester FROM blood_requests br JOIN users u ON u.id=br.requester_id WHERE br.id=?",(request_id,),one=True)
    if not r or (u["role"]=="recipient" and r["requester_id"]!=u["id"]): return redirect(url_for("dashboard"))
    match_request(request_id)
    inv=q("""SELECT i.*,d.name donor,d.blood_group FROM invitations i JOIN users d ON d.id=i.donor_id
             WHERE i.request_id=? ORDER BY i.distance_km IS NULL,i.distance_km""",(request_id,))
    return render_template("request-status.html",r=r,invitations=inv,current_radius=current_radius(r))

@app.route("/matched-donors")
@role_required("hospital","recipient")
def matched_donors():
    u=current_user()
    reqs=q("SELECT * FROM blood_requests WHERE requester_id=? ORDER BY created_at DESC",(u["id"],))
    for r in reqs: match_request(r["id"])
    rid=request.args.get("request_id",type=int)
    if not rid and reqs: rid=reqs[0]["id"]
    r=q("SELECT * FROM blood_requests WHERE id=?",(rid,),one=True) if rid else None
    inv=q("""SELECT i.*,d.name donor,d.blood_group,d.phone FROM invitations i JOIN users d ON d.id=i.donor_id
             WHERE i.request_id=? ORDER BY i.distance_km""",(rid,)) if rid else []
    return render_template("matched-donors.html",r=r,invitations=inv,requests=reqs)

@app.route("/api/invitations/<int:iid>/<action>",methods=["POST"])
@login_required
def invitation_action(iid,action):
    u=current_user()
    inv=q("SELECT * FROM invitations WHERE id=?",(iid,),one=True)
    if not inv: return jsonify(error="Invitation not found"),404
    if action not in ("accept","reject") or (u["role"]!="donor" or inv["donor_id"]!=u["id"]):
        return jsonify(error="Not allowed"),403
    if inv["status"]!="Not Responded": return jsonify(error="Invitation already responded to"),400
    status="Accepted" if action=="accept" else "Rejected"
    execute("UPDATE invitations SET status=?,responded_at=? WHERE id=?",(status,now(),iid))
    if status=="Accepted":
        execute("UPDATE blood_requests SET status='Committed',updated_at=? WHERE id=?",(now(),inv["request_id"]))
    log(u["id"],"INVITATION_"+status.upper(),f"Invitation #{iid}")
    return jsonify(ok=True,status=status)

@app.route("/api/requests/<int:rid>/state",methods=["POST"])
@login_required
def request_state(rid):
    u=current_user(); r=q("SELECT * FROM blood_requests WHERE id=?",(rid,),one=True)
    if not r: return jsonify(error="Not found"),404
    if u["role"] not in ("hospital","admin") and r["requester_id"]!=u["id"]: return jsonify(error="Not allowed"),403
    target=request.json.get("state") if request.is_json else request.form.get("state")
    valid={
      "Active":{"Notified","Cancelled","Expired"}, "Notified":{"Committed","Cancelled","Expired"},
      "Committed":{"Confirmed","Cancelled","Expired"}, "Confirmed":{"Donated","Cancelled"},
      "Donated":set(),"Cancelled":set(),"Expired":set()
    }
    if target not in valid.get(r["status"],set()): return jsonify(error=f"Invalid transition {r['status']} -> {target}"),400
    execute("UPDATE blood_requests SET status=?,updated_at=? WHERE id=?",(target,now(),rid))
    log(u["id"],"REQUEST_STATE",f"#{rid}: {r['status']} -> {target}")
    return jsonify(ok=True,status=target)

@app.route("/api/matching/<int:rid>",methods=["POST"])
@login_required
def run_matching(rid):
    u=current_user(); r=q("SELECT * FROM blood_requests WHERE id=?",(rid,),one=True)
    if not r: return jsonify(error="Not found"),404
    if u["role"] not in ("admin","hospital") and r["requester_id"]!=u["id"]: return jsonify(error="Not allowed"),403
    out=match_request(rid)
    return jsonify(ok=True,radius_km=current_radius(r),new_candidates=len(out))

@app.route("/hospital-dashboard")
@role_required("hospital")
def hospital_dashboard():
    u=current_user()
    stats=(q("SELECT COUNT(*) n FROM blood_requests WHERE requester_id=? AND status NOT IN ('Donated','Cancelled','Expired')",(u["id"],),one=True)["n"],
           q("SELECT COUNT(*) n FROM invitations i JOIN blood_requests r ON r.id=i.request_id WHERE r.requester_id=?",(u["id"],),one=True)["n"],
           q("SELECT COUNT(*) n FROM invitations i JOIN blood_requests r ON r.id=i.request_id WHERE r.requester_id=? AND i.status='Accepted'",(u["id"],),one=True)["n"],
           q("SELECT COUNT(*) n FROM organ_pledges WHERE status='Active'",(),one=True)["n"])
    return render_template("hospital-dashboard.html",stats=stats)

@app.route("/admin-dashboard")
@role_required("admin")
def admin_dashboard():
    stats=(q("SELECT COUNT(*) n FROM users",one=True)["n"],q("SELECT COUNT(*) n FROM users WHERE role='hospital'",one=True)["n"],
           q("SELECT COUNT(*) n FROM blood_requests WHERE status NOT IN ('Donated','Cancelled','Expired')",one=True)["n"],
           q("SELECT COUNT(*) n FROM users WHERE role='hospital' AND verified=0",one=True)["n"])
    return render_template("admin-dashboard.html",stats=stats)

@app.route("/manage-users")
@role_required("admin")
def manage_users(): return render_template("manage-users.html",users=q("SELECT * FROM users ORDER BY created_at DESC"))

@app.route("/manage-hospitals")
@role_required("admin")
def manage_hospitals(): return render_template("manage-hospitals.html",hospitals=q("SELECT * FROM users WHERE role='hospital' ORDER BY verified,created_at DESC"))

@app.route("/api/hospitals/<int:uid>/verify",methods=["POST"])
@role_required("admin")
def verify_hospital(uid):
    h=q("SELECT * FROM users WHERE id=? AND role='hospital'",(uid,),one=True)
    if not h:return jsonify(error="Hospital not found"),404
    execute("UPDATE users SET verified=1 WHERE id=?",(uid,)); log(current_user()["id"],"HOSPITAL_VERIFIED",str(uid))
    return jsonify(ok=True)

@app.route("/donation-rules")
@login_required
def donation_rules(): return render_template("donation-rules.html",cooldown=COOLDOWN_DAYS,radii=RADIUS,expansion=EXPANSION_MINUTES)

@app.route("/system-activity")
@role_required("admin")
def system_activity(): return render_template("system-activity.html",activities=q("""SELECT a.*,u.name actor FROM activity a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.created_at DESC LIMIT 100"""))

@app.route("/organ-pledge",methods=["GET","POST"])
@role_required("donor")
def organ_pledge():
    u=current_user()
    if request.method=="POST":
        organ=request.form.get("organ","").strip(); status=request.form.get("status","Active")
        if not organ or status not in ("Active","Withdrawn") or not request.form.get("consent"):
            flash("Select an organ/category and accept the pledge declaration.","error")
        else:
            existing=q("SELECT id FROM organ_pledges WHERE donor_id=? AND status='Active'",(u["id"],),one=True)
            if existing:
                execute("UPDATE organ_pledges SET organ=?,status=?,emergency_contact=?,consent=1,updated_at=? WHERE id=?",
                        (organ,status,request.form.get("emergency_contact","").strip(),now(),existing["id"]))
            else:
                execute("INSERT INTO organ_pledges(donor_id,organ,status,emergency_contact,consent,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (u["id"],organ,status,request.form.get("emergency_contact","").strip(),1,now(),now()))
            log(u["id"],"ORGAN_PLEDGE_SAVED",organ); flash("Organ pledge saved.","success")
            return redirect(url_for("organ_pledge"))
    pledge=q("SELECT * FROM organ_pledges WHERE donor_id=? ORDER BY updated_at DESC LIMIT 1",(u["id"],),one=True)
    return render_template("organ-pledge.html",pledge=pledge)

@app.route("/organ-connections")
@role_required("hospital")
def organ_connections():
    pledges=q("""SELECT p.*,d.name donor,d.phone,d.city FROM organ_pledges p JOIN users d ON d.id=p.donor_id
                 WHERE p.status='Active' ORDER BY p.updated_at DESC""")
    conns={x["pledge_id"]:x for x in q("SELECT * FROM organ_connections WHERE hospital_id=?",(current_user()["id"],))}
    return render_template("organ-connections.html",pledges=pledges,connections=conns)

@app.route("/api/organ/<int:pid>/connect",methods=["POST"])
@role_required("hospital")
def connect_organ(pid):
    p=q("SELECT * FROM organ_pledges WHERE id=? AND status='Active'",(pid,),one=True)
    if not p:return jsonify(error="Pledge not found"),404
    execute("INSERT OR IGNORE INTO organ_connections(pledge_id,hospital_id,status,created_at) VALUES(?,?,?,?)",
            (pid,current_user()["id"],"Connected",now()))
    log(current_user()["id"],"ORGAN_CONNECTED",f"Pledge #{pid}")
    return jsonify(ok=True)

@app.route("/camps")
@role_required("camp_organizer")
def camps(): return render_template("camps.html",camps=q("SELECT * FROM camps WHERE organizer_id=? ORDER BY event_date",(current_user()["id"],)))

@app.route("/camp/<int:cid>")
@role_required("camp_organizer")
def camp_detail(cid):
    c=q("SELECT * FROM camps WHERE id=? AND organizer_id=?",(cid,current_user()["id"]),one=True)
    if not c:return redirect(url_for("camps"))
    return render_template("camp-detail.html",camp=c,targets=q("SELECT * FROM camp_targets WHERE camp_id=?",(cid,)))

@app.route("/camps/new",methods=["POST"])
@role_required("camp_organizer")
def create_camp():
    cid=execute("INSERT INTO camps(organizer_id,name,location,event_date,created_at) VALUES(?,?,?,?,?)",
                (current_user()["id"],request.form["name"].strip(),request.form["location"].strip(),request.form["event_date"],now()))
    for bg in BLOOD_GROUPS:
        val=request.form.get("target_"+bg)
        if val and int(val)>0: execute("INSERT INTO camp_targets(camp_id,blood_group,target_units) VALUES(?,?,?)",(cid,bg,int(val)))
    log(current_user()["id"],"CAMP_CREATED",str(cid)); return redirect(url_for("camp_detail",cid=cid))

@app.route("/camp/<int:cid>/target",methods=["POST"])
@role_required("camp_organizer")
def update_target(cid):
    c=q("SELECT * FROM camps WHERE id=? AND organizer_id=?",(cid,current_user()["id"]),one=True)
    if not c:return jsonify(error="Not found"),404
    bg=request.form.get("blood_group"); target=int(request.form.get("target",0))
    execute("""INSERT INTO camp_targets(camp_id,blood_group,target_units,collected_units) VALUES(?,?,?,0)
               ON CONFLICT(camp_id,blood_group) DO UPDATE SET target_units=excluded.target_units""",(cid,bg,target))
    return redirect(url_for("camp_detail",cid=cid))

@app.route("/camp/<int:cid>/donation",methods=["POST"])
@role_required("camp_organizer")
def camp_donation(cid):
    c=q("SELECT * FROM camps WHERE id=? AND organizer_id=?",(cid,current_user()["id"]),one=True)
    if not c:return jsonify(error="Not found"),404
    donor_id=request.form.get("donor_id",type=int)
    bg=request.form.get("blood_group")
    units=max(1,request.form.get("units",1,type=int))
    donor=q("SELECT * FROM users WHERE id=? AND role='donor'",(donor_id,),one=True)
    if not donor or donor["blood_group"]!=bg:
        flash("A valid donor ID with the selected blood group is required.","error")
    else:
        did=execute("""INSERT INTO donations(donor_id,request_id,donation_type,donation_date,units,camp_id,verifying_org_id,verified_at)
                       VALUES(?,?,?,?,?,?,?,?)""",(donor_id,None,"Blood",datetime.utcnow().date().isoformat(),units,cid,current_user()["id"],now()))
        execute("UPDATE users SET last_donation=? WHERE id=?",(datetime.utcnow().date().isoformat(),donor_id))
        log(current_user()["id"],"CAMP_DONATION_VERIFIED",f"Camp #{cid}, donation #{did}")
        for t in q("SELECT * FROM camp_targets WHERE camp_id=?",(cid,)):
            total=q("SELECT COALESCE(SUM(d.units),0) n FROM donations d JOIN users du ON du.id=d.donor_id WHERE d.camp_id=? AND d.donation_type='Blood' AND du.blood_group=?",(cid,t["blood_group"]),one=True)["n"]
            execute("UPDATE camp_targets SET collected_units=? WHERE id=?",(min(total,t["target_units"]),t["id"]))
        flash("Verified donation added to camp progress.","success")
    return redirect(url_for("camp_detail",cid=cid))

@app.route("/api/donations/verify",methods=["POST"])
@role_required("hospital","camp_organizer")
def verify_donation():
    donor_id=request.form.get("donor_id",type=int); request_id=request.form.get("request_id",type=int)
    donation_date=request.form.get("donation_date") or datetime.utcnow().date().isoformat()
    donation_type=request.form.get("donation_type","Blood")
    units=max(1,request.form.get("units",1,type=int))
    d=q("SELECT * FROM users WHERE id=? AND role='donor'",(donor_id,),one=True)
    if not d:return jsonify(error="Donor not found"),404
    camp_id=request.form.get("camp_id",type=int)
    did=execute("""INSERT INTO donations(donor_id,request_id,donation_type,donation_date,units,camp_id,verifying_org_id,verified_at)
                   VALUES(?,?,?,?,?,?,?,?)""",(donor_id,request_id or None,donation_type,donation_date,units,camp_id,current_user()["id"],now()))
    execute("UPDATE users SET last_donation=? WHERE id=?",(donation_date,donor_id))
    if request_id:
        r=q("SELECT status FROM blood_requests WHERE id=?",(request_id,),one=True)
        if r and r["status"]=="Confirmed": execute("UPDATE blood_requests SET status='Donated',updated_at=? WHERE id=?",(now(),request_id))
    log(current_user()["id"],"DONATION_VERIFIED",f"Donation #{did} for donor {donor_id}")
    return jsonify(ok=True,donation_id=did)

@app.route("/history")
@role_required("donor","hospital","camp_organizer")
def history():
    u=current_user()
    if u["role"]=="donor":
        donations=q("""SELECT d.*,o.name org FROM donations d JOIN users o ON o.id=d.verifying_org_id WHERE d.donor_id=? ORDER BY donation_date DESC""",(u["id"],))
    else:
        donations=q("""SELECT d.*,u.name donor,o.name org FROM donations d JOIN users u ON u.id=d.donor_id JOIN users o ON o.id=d.verifying_org_id
                       WHERE d.verifying_org_id=? ORDER BY donation_date DESC""",(u["id"],))
    return render_template("history.html",donations=donations)

@app.route("/camp-dashboard")
@role_required("camp_organizer")
def camp_dashboard(): return redirect(url_for("camps"))

@app.errorhandler(404)
def not_found(e): return render_template("error.html",code=404,message="Page not found"),404
@app.errorhandler(500)
def server_error(e): return render_template("error.html",code=500,message="Something went wrong on the server"),500

with app.app_context(): init_db()

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
