import os
from datetime import datetime, timedelta
from pymongo import MongoClient
from pymongo import ReturnDocument

# ══ CONNECT TO REAL MONGODB (Atlas) ONLY ══
# No local file fallback: the bot always talks to the real MongoDB
# configured via MONGO_URI in .env.
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is not set. Set the real MongoDB URI in .env")
client = MongoClient(MONGO_URI)
db = client["adwa_bingo"]

# Collections (Like Tables)
users_col = db["users"]
transactions_col = db["transactions"]
game_sessions_col = db["game_sessions"]
counters_col = db["counters"]

# ══ MULTIPLAYER COLLECTIONS (Step 6 upgrade) ══
games_col = db["games"]                 # one document per multiplayer game
game_players_col = db["game_players"]   # player membership per game
bingo_cards_col = db["bingo_cards"]     # server-generated cards per game
game_history_col = db["game_history"]   # finished games archive
open_rooms_col = db["open_rooms"]       # one doc per stake: {stake, game_id} —
                                         # the ONE shared pointer to "which room
                                         # is currently open", read/written by
                                         # every server process/instance

_db_source = globals().get('MONGO_URI') or 'local'
print(f"✅ Connected to MongoDB ({_db_source.split('@')[-1].split('/')[0]})")

# ══ HELPER FOR AUTO-INCREMENT IDS ══
def get_next_id(name):
    result = counters_col.find_one_and_update(
        {"_id": name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True
    )
    return result["seq"]


# ==========================================
# USER FUNCTIONS
# ==========================================

def add_user(user_id, phone='', first_name='', referred_by=None, username=''):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    users_col.update_one(
        {"user_id": user_id},
        {"$setOnInsert": {"phone": phone, "first_name": first_name, "username": username, "referred_by": referred_by, "status": "active", "created_at": now, "main_balance": 0, "play_balance": 0, "is_agent": 0, "is_vip": 0, "language": "am"}},
        upsert=True
    )
    if first_name:
        users_col.update_one({"user_id": user_id, "$or": [{"first_name": None}, {"first_name": ""}]}, {"$set": {"first_name": first_name}})
    if username:
        users_col.update_one({"user_id": user_id}, {"$set": {"username": username}})

def update_user_username(user_id, username):
    if username:
        users_col.update_one({"user_id": user_id}, {"$set": {"username": username}})
def update_user_name(user_id, first_name):
    users_col.update_one({"user_id": user_id}, {"$set": {"first_name": first_name}})

def grant_welcome_bonus(user_id, amount=1000):
    """Deprecated: welcome bonuses are disabled. Kept as a no-op so legacy
    callers (if any) never crash; nothing grants balance anymore."""
    return False

def user_exists(user_id):
    return users_col.find_one({"user_id": user_id}) is not None

def has_valid_phone(user_id):
    """True when the user completed phone registration (fixes bot import)."""
    u = users_col.find_one({"user_id": user_id}, {"phone": 1})
    return bool(u and u.get("phone"))

def get_user(user_id):
    u = users_col.find_one({"user_id": user_id})
    if not u: return None
    return (u.get("user_id"), u.get("phone"), u.get("main_balance", 0), u.get("play_balance", 0), u.get("referred_by"))

def get_user_full(user_id):
    u = users_col.find_one({"user_id": user_id})
    if not u: return None
    return {
        'user_id': u.get("user_id"),
        'first_name': u.get("first_name", ''),
        'phone': u.get("phone", ''),
        'main_balance': u.get("main_balance", 0),
        'play_balance': u.get("play_balance", 0),
        'referred_by': u.get("referred_by"),
        'is_agent': u.get("is_agent", 0),
        'is_vip': u.get("is_vip", 0),
        'language': u.get("language", 'am'),
        'status': u.get("status", 'active'),
        'created_at': u.get("created_at", ''),
    }

def get_user_name(user_id):
    u = users_col.find_one({"user_id": user_id})
    return u.get("first_name", 'User') if u else 'User'

def get_user_phone(user_id):
    u = users_col.find_one({"user_id": user_id})
    return u.get("phone") if u else None


# ==========================================
# BALANCE FUNCTIONS
# ==========================================

def get_main_balance(user_id):
    u = users_col.find_one({"user_id": user_id})
    return u.get("main_balance", 0) if u else 0

def update_main_balance(user_id, amount):
    users_col.update_one({"user_id": user_id}, {"$inc": {"main_balance": amount}})
    u = users_col.find_one({"user_id": user_id})
    return u.get("main_balance", 0) if u else 0

def get_play_balance(user_id):
    u = users_col.find_one({"user_id": user_id})
    return u.get("play_balance", 0) if u else 0

def update_play_balance(user_id, amount):
    users_col.update_one({"user_id": user_id}, {"$inc": {"play_balance": amount}})
    u = users_col.find_one({"user_id": user_id})
    return u.get("play_balance", 0) if u else 0

def deduct_bet_smart(user_id, amount):
    """
    Deduct `amount`, using play_balance first and spilling into
    main_balance for any remainder - same policy as before, but now done
    with conditional atomic updates instead of read-then-write. Two
    requests from the same user landing at nearly the same instant (double
    tap, two tabs, a retried request) can no longer both read "enough
    funds" and both succeed, which is how a balance could go negative
    under the old read/decide/write version.
    """
    doc = users_col.find_one_and_update(
        {"user_id": user_id, "play_balance": {"$gte": amount}},
        {"$inc": {"play_balance": -amount}},
        return_document=ReturnDocument.AFTER,
    )
    if doc:
        return {'main_balance': doc.get('main_balance', 0), 'play_balance': doc.get('play_balance', 0)}

    user = users_col.find_one({"user_id": user_id})
    if not user:
        return False
    play_bal = max(user.get('play_balance', 0), 0)
    main_bal = user.get('main_balance', 0)
    remaining = amount - play_bal
    if remaining < 0 or play_bal + main_bal < amount:
        return False
    doc = users_col.find_one_and_update(
        {"user_id": user_id, "play_balance": play_bal, "main_balance": {"$gte": remaining}},
        {"$inc": {"play_balance": -play_bal, "main_balance": -remaining}},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        return False   # balance changed between our read and write - fail safe, caller treats as insufficient funds
    return {'main_balance': doc.get('main_balance', 0), 'play_balance': doc.get('play_balance', 0)}


# ==========================================
# REFERRAL FUNCTIONS
# ==========================================

def set_referral(user_id, referred_by):
    users_col.update_one({"user_id": user_id, "referred_by": None}, {"$set": {"referred_by": referred_by}})

def get_referral_count(user_id):
    return users_col.count_documents({"referred_by": user_id})

def get_depositing_referrals_count(user_id):
    invited_ids = [u["user_id"] for u in users_col.find({"referred_by": user_id}, {"user_id": 1})]
    # DISTINCT users who deposited (not the number of deposit transactions)
    return len(transactions_col.distinct("user_id", {"user_id": {"$in": invited_ids}, "type": "deposit", "status": "completed"}))

def get_total_referral_deposits(user_id):
    invited_ids = [u["user_id"] for u in users_col.find({"referred_by": user_id}, {"user_id": 1})]
    pipeline = [
        {"$match": {"user_id": {"$in": invited_ids}, "type": "deposit", "status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    result = list(transactions_col.aggregate(pipeline))
    return result[0]["total"] if result else 0


# ==========================================
# TRANSACTION FUNCTIONS
# ==========================================

def add_transaction(user_id, tx_type, amount, method="System", tx_id=None, status='completed', row_id=None):
    if tx_id is None and tx_type in ('deposit', 'withdraw'):
        tx_id = f"{tx_type.upper()}_{user_id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    if row_id is None:
        row_id = get_next_id("transactions")   # new number; a caller can pass its own to keep an earlier number
    transactions_col.insert_one({
        "id": row_id, "user_id": user_id, "type": tx_type, "amount": amount,
        "method": method, "tx_id": tx_id, "status": status, "time": now
    })
    return row_id

def update_transaction_status(tx_id_or_id, new_status):
    if isinstance(tx_id_or_id, int):
        result = transactions_col.update_one({"id": tx_id_or_id}, {"$set": {"status": new_status}})
    else:
        result = transactions_col.update_one({"tx_id": tx_id_or_id}, {"$set": {"status": new_status}})
    return result.modified_count > 0

def get_transaction_by_id(tx_id):
    if isinstance(tx_id, int):
        t = transactions_col.find_one({"id": tx_id})
    else:
        t = transactions_col.find_one({"tx_id": tx_id})
    return t

def get_last_5_transactions(user_id):
    txs = transactions_col.find({"user_id": user_id, "status": "completed", "type": {"$in": ["deposit", "withdraw"]}}).sort("id", -1).limit(5)
    return [(t["type"], t["amount"], t["time"]) for t in txs]

def get_all_transactions(user_id, limit=20):
    txs = transactions_col.find({"user_id": user_id}).sort("id", -1).limit(limit)
    return [(t["type"], t["amount"], t["status"], t["time"]) for t in txs]

def get_total_deposits(user_id):
    pipeline = [
        {"$match": {"user_id": user_id, "type": "deposit", "status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    result = list(transactions_col.aggregate(pipeline))
    return result[0]["total"] if result else 0

def transaction_exists(tx_id):
    if not tx_id: return False
    return transactions_col.find_one({"tx_id": tx_id}) is not None


# ==========================================
# DEPOSIT FUNCTIONS (Admin)
# ==========================================

def add_pending_deposit(user_id, amount, method='Telebirr', tx_id=None, phone=None):
    if phone:
        users_col.update_one({"user_id": user_id, "$or": [{"phone": None}, {"phone": ""}]}, {"$set": {"phone": phone}})
    return add_transaction(user_id, 'deposit', amount, method, tx_id, status='pending')

def approve_deposit(transaction_id):
    t = transactions_col.find_one({"id": transaction_id})
    if not t: return False, None, 0
    if t["status"] != 'pending': return False, t["user_id"], t["amount"]
    transactions_col.update_one({"id": transaction_id}, {"$set": {"status": "completed"}})
    users_col.update_one({"user_id": t["user_id"]}, {"$inc": {"play_balance": t["amount"]}})
    return True, t["user_id"], t["amount"]

def reject_deposit(transaction_id):
    t = transactions_col.find_one({"id": transaction_id})
    if not t: return False, None
    if t["status"] != 'pending': return False, t["user_id"]
    transactions_col.update_one({"id": transaction_id}, {"$set": {"status": "rejected"}})
    return True, t["user_id"]

def get_all_deposits(limit=200):
    pipeline = [
        {"$match": {"type": "deposit"}},
        {"$sort": {"id": -1}},
        {"$limit": limit},
        {"$lookup": {"from": "users", "localField": "user_id", "foreignField": "user_id", "as": "user_info"}},
        {"$unwind": {"path": "$user_info", "preserveNullAndEmptyArrays": True}}
    ]
    deposits = []
    for t in transactions_col.aggregate(pipeline):
        u = t.get("user_info") or {}
        deposits.append({
            'id': t["id"], 'user_id': t["user_id"], 'username': u.get("first_name", '—'),
            'phone': u.get("phone", '—'), 'amount': t["amount"], 'method': t.get("method", 'Telebirr'),
            'tx_id': t.get("tx_id", '—'), 'status': t.get("status", 'pending'), 'time': t.get("time", '')
        })
    return deposits

def get_pending_deposits_count():
    return transactions_col.count_documents({"type": "deposit", "status": "pending"})


# ==========================================
# WITHDRAWAL FUNCTIONS (Admin)
# ==========================================

def add_pending_withdrawal(user_id, amount, method='Telebirr', phone=None):
    main_bal = get_main_balance(user_id)
    if main_bal < amount: return None
    update_main_balance(user_id, -amount)
    return add_transaction(user_id, 'withdraw', amount, method, status='pending')

def approve_withdrawal(transaction_id):
    t = transactions_col.find_one({"id": transaction_id})
    if not t: return False, None, 0
    if t["status"] != 'pending': return False, t["user_id"], t["amount"]
    transactions_col.update_one({"id": transaction_id}, {"$set": {"status": "completed"}})
    return True, t["user_id"], t["amount"]

def reject_withdrawal(transaction_id):
    t = transactions_col.find_one({"id": transaction_id})
    if not t: return False, None, 0
    if t["status"] != 'pending': return False, t["user_id"], t["amount"]
    transactions_col.update_one({"id": transaction_id}, {"$set": {"status": "rejected"}})
    users_col.update_one({"user_id": t["user_id"]}, {"$inc": {"main_balance": t["amount"]}})
    return True, t["user_id"], t["amount"]

def get_user_stats_map(user_ids):
    """Real numbers for a group of users in a FIXED number of queries (not 3 per
    row): name, balances, games played / won and invites. Used by the Withdrawals
    table so every row shows real figures without opening the Users page first."""
    ids = list({i for i in user_ids if i is not None})
    stats = {i: {'first_name': '', 'main_balance': 0, 'play_balance': 0, 'games_played': 0,
                 'games_won': 0, 'referral_count': 0, 'is_agent': 0} for i in ids}
    if not ids:
        return stats
    for u in users_col.find({"user_id": {"$in": ids}}):
        s = stats[u["user_id"]]
        s['first_name'] = u.get("first_name", '') or ''
        s['main_balance'] = u.get("main_balance", 0)
        s['play_balance'] = u.get("play_balance", 0)
        s['is_agent'] = u.get("is_agent", 0)
    for r in game_sessions_col.aggregate([{"$match": {"user_id": {"$in": ids}}},
                                          {"$group": {"_id": "$user_id", "n": {"$sum": 1}}}]):
        stats[r["_id"]]['games_played'] = r["n"]
    for r in game_sessions_col.aggregate([{"$match": {"user_id": {"$in": ids}, "status": "Won"}},
                                          {"$group": {"_id": "$user_id", "n": {"$sum": 1}}}]):
        stats[r["_id"]]['games_won'] = r["n"]
    for r in users_col.aggregate([{"$match": {"referred_by": {"$in": ids}}},
                                  {"$group": {"_id": "$referred_by", "n": {"$sum": 1}}}]):
        stats[r["_id"]]['referral_count'] = r["n"]
    return stats

def get_all_withdrawals(limit=100):
    pipeline = [
        {"$match": {"type": "withdraw"}},
        {"$sort": {"id": -1}},
        {"$limit": limit},
        {"$lookup": {"from": "users", "localField": "user_id", "foreignField": "user_id", "as": "user_info"}},
        {"$unwind": {"path": "$user_info", "preserveNullAndEmptyArrays": True}}
    ]
    withdrawals = []
    for t in transactions_col.aggregate(pipeline):
        u = t.get("user_info") or {}
        withdrawals.append({
            'id': t["id"], 'user_id': t["user_id"], 'username': u.get("first_name", '—'),
            'phone': u.get("phone", '—'), 'amount': t["amount"], 'method': t.get("method", 'Telebirr'),
            'status': t.get("status", 'pending'), 'time': t.get("time", '')
        })
    stats = get_user_stats_map([w['user_id'] for w in withdrawals])
    for w in withdrawals:
        w.update(stats.get(w['user_id'], {}))
    return withdrawals

def get_pending_withdrawals_count():
    return transactions_col.count_documents({"type": "withdraw", "status": "pending"})


# ==========================================
# GAME SESSION FUNCTIONS
# ==========================================

def add_game_session(user_id, game_id, cards, entry_amount=10):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(cards, list) and len(cards) > 0:
        for card in cards:
            game_sessions_col.insert_one({
                "user_id": user_id, "game_id": game_id, "cards": str(card),
                "entry_amount": entry_amount, "status": "playing", "result": "-",
                "prize": 0, "time": now
            })
    else:
        game_sessions_col.insert_one({
            "user_id": user_id, "game_id": game_id, "cards": str(cards),
            "entry_amount": entry_amount, "status": "playing", "result": "-",
            "prize": 0, "time": now
        })

def complete_game_session(user_id, game_id, result='-', prize=0, cards='-', entry_amount=0):
    status = 'Won' if prize > 0 else 'Completed'
    result_str = result if prize > 0 else '-'
    game_sessions_col.update_one(
        {"user_id": user_id, "game_id": game_id, "cards": str(cards)},
        {"$set": {
            "user_id": user_id, "game_id": game_id, "cards": str(cards),
            "entry_amount": entry_amount, "status": status, "result": result_str,
            "prize": prize, "time": datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        }},
        upsert=True
    )

def get_game_history(user_id, limit=20):
    sessions = game_sessions_col.find({"user_id": user_id}).sort("time", -1).limit(limit)
    return [(s["game_id"], s["entry_amount"], s["status"], s["result"], s["time"]) for s in sessions]

def get_games_played_count(user_id):
    return game_sessions_col.count_documents({"user_id": user_id})

def get_total_bet(user_id):
    return sum(t.get("amount", 0) for t in transactions_col.find({"user_id": user_id, "type": "bingo_bet"}))

def get_last_played(user_id):
    sessions = game_sessions_col.find({"user_id": user_id}).sort("time", -1).limit(1)
    for s in sessions:
        return s.get("time")
    return None

def get_games_won_count(user_id):
    return game_sessions_col.count_documents({"user_id": user_id, "status": "Won"})

def get_total_won(user_id):
    pipeline = [
        {"$match": {"user_id": user_id, "status": "Won"}},
        {"$group": {"_id": None, "total": {"$sum": "$prize"}}}
    ]
    result = list(game_sessions_col.aggregate(pipeline))
    return result[0]["total"] if result else 0


# ==========================================
# PROFILE STATS
# ==========================================

def get_profile_stats(user_id):
    return {
        'games_played': get_games_played_count(user_id),
        'games_won': get_games_won_count(user_id),
        'total_won': get_total_won(user_id),
        'invited': get_referral_count(user_id),
    }


# ==========================================
# LEADERBOARD FUNCTIONS
# ==========================================

def get_period_start(period):
    now = datetime.utcnow()
    if period == 'week':
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == 'month':
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now - timedelta(days=7)
    return start.strftime('%Y-%m-%d %H:%M:%S')

def get_top_by_deposit(period='week', limit=30):
    since = get_period_start(period)
    pipeline = [
        {"$match": {"type": "deposit", "status": "completed", "time": {"$gte": since}}},
        {"$group": {"_id": "$user_id", "total_amount": {"$sum": "$amount"}, "times": {"$sum": 1}}},
        {"$sort": {"total_amount": -1}},
        {"$limit": limit},
        {"$lookup": {"from": "users", "localField": "_id", "foreignField": "user_id", "as": "user_info"}},
        {"$unwind": "$user_info"}
    ]
    return [(r["_id"], r["user_info"].get("first_name", "User"), r["total_amount"], r["times"]) for r in transactions_col.aggregate(pipeline)]

def get_top_by_invitations(period='week', limit=30):
    since = get_period_start(period)
    pipeline = [
        {"$match": {"created_at": {"$gte": since}}},
        {"$group": {"_id": "$referred_by", "total": {"$sum": 1}}},
        {"$match": {"_id": {"$ne": None}, "total": {"$gt": 0}}},
        {"$sort": {"total": -1}},
        {"$limit": limit},
        {"$lookup": {"from": "users", "localField": "_id", "foreignField": "user_id", "as": "user_info"}},
        {"$unwind": "$user_info"}
    ]
    return [(r["_id"], r["user_info"].get("first_name", "User"), r["total"]) for r in users_col.aggregate(pipeline)]

def get_top_by_games(period='week', limit=30):
    since = get_period_start(period)
    pipeline = [
        {"$match": {"time": {"$gte": since}}},
        {"$group": {"_id": "$user_id", "total": {"$sum": 1}}},
        {"$sort": {"total": -1}},
        {"$limit": limit},
        {"$lookup": {"from": "users", "localField": "_id", "foreignField": "user_id", "as": "user_info"}},
        {"$unwind": "$user_info"}
    ]
    return [(r["_id"], r["user_info"].get("first_name", "User"), r["total"]) for r in game_sessions_col.aggregate(pipeline)]

def get_top_by_wins(period='week', limit=30):
    since = get_period_start(period)
    pipeline = [
        {"$match": {"status": "Won", "time": {"$gte": since}}},
        {"$group": {"_id": "$user_id", "total": {"$sum": 1}}},
        {"$sort": {"total": -1}},
        {"$limit": limit},
        {"$lookup": {"from": "users", "localField": "_id", "foreignField": "user_id", "as": "user_info"}},
        {"$unwind": "$user_info"}
    ]
    return [(r["_id"], r["user_info"].get("first_name", "User"), r["total"]) for r in game_sessions_col.aggregate(pipeline)]

def get_user_rank(user_id, period='week', category='deposit'):
    if category == 'deposit':
        rows = get_top_by_deposit(period, 1000)
        for i, row in enumerate(rows):
            if row[0] == user_id: return i + 1, row[3]
        return None, 0
    elif category == 'invite': rows = get_top_by_invitations(period, 1000)
    elif category == 'wins': rows = get_top_by_wins(period, 1000)
    else: rows = get_top_by_games(period, 1000)
    
    for i, row in enumerate(rows):
        if row[0] == user_id: return i + 1, row[2]
    return None, 0
    
    for i, row in enumerate(rows):
        if row[0] == user_id: return i + 1, row[2]
    return None, 0


# ==========================================
# ADMIN: USER MANAGEMENT
# ==========================================

def get_all_users_with_stats(limit=500):
    users = list(users_col.find().sort("user_id", -1).limit(limit))
    result = []
    for u in users:
        result.append({
            'user_id': u.get("user_id"),
            'first_name': u.get("first_name", '—'),
            'username': u.get("username", ''),
            'phone': u.get("phone", '—'),
            'main_balance': u.get("main_balance", 0),
            'play_balance': u.get("play_balance", 0),
            'is_agent': u.get("is_agent", 0),
            'is_vip': u.get("is_vip", 0),
            'language': u.get("language", 'am'),
            'status': u.get("status", 'active'),
            'games_played': game_sessions_col.count_documents({"user_id": u["user_id"]}),
            'games_won': game_sessions_col.count_documents({"user_id": u["user_id"], "status": "Won"}),
            'referral_count': users_col.count_documents({"referred_by": u["user_id"]}),
        })
    return result

def ban_user(user_id):
    result = users_col.update_one({"user_id": user_id}, {"$set": {"status": "banned"}})
    return result.modified_count > 0

def unban_user(user_id):
    result = users_col.update_one({"user_id": user_id}, {"$set": {"status": "active"}})
    return result.modified_count > 0

def mark_vip(user_id, vip=True):
    result = users_col.update_one({"user_id": user_id}, {"$set": {"is_vip": 1 if vip else 0}})
    return result.modified_count > 0

def is_user_banned(user_id):
    u = users_col.find_one({"user_id": user_id})
    return u and u.get("status") == "banned"

def is_user_vip(user_id):
    u = users_col.find_one({"user_id": user_id})
    return bool(u and u.get("is_vip") == 1)

def is_user_frozen(user_id):
    u = users_col.find_one({"user_id": user_id})
    return u and u.get("status") == "frozen"

def freeze_user(user_id):
    result = users_col.update_one({"user_id": user_id}, {"$set": {"status": "frozen"}})
    return result.modified_count > 0

def unfreeze_user(user_id):
    result = users_col.update_one({"user_id": user_id}, {"$set": {"status": "active"}})
    return result.modified_count > 0


# ==========================================
# ADMIN: DASHBOARD STATS
# ==========================================

def get_dashboard_stats():
    today = datetime.utcnow().strftime('%Y-%m-%d')
    total_users = users_col.count_documents({})
    
    def sum_transactions(tx_type, status='completed', date_prefix=None):
        match = {"type": tx_type, "status": status}
        if date_prefix: match["time"] = {"$regex": f"^{date_prefix}"}
        pipeline = [{"$match": match}, {"$group": {"_id": None, "total": {"$sum": "$amount"}}}]
        res = list(transactions_col.aggregate(pipeline))
        return res[0]["total"] if res else 0

    today_deposits = sum_transactions('deposit', 'completed', today)
    today_withdrawals = sum_transactions('withdraw', 'completed', today)
    today_payout = sum_transactions('bingo_win', 'completed', today)
    games_today = game_sessions_col.count_documents({"time": {"$regex": f"^{today}"}})

    # Today's PROFIT = the real 20% house take, summed across every game
    # that finished today (same calculation as Game History), NOT
    # deposits minus payout — those two have nothing to do with each other.
    today_profit = 0
    for h in game_history_col.find({"date": {"$regex": f"^{today}"}}):
        game_id = h.get("game_id")
        stake = h.get("stake", 10)
        try:
            cards = bingo_cards_col.count_documents({"game_id": game_id})
        except Exception:
            cards = 0
        if cards == 0:
            cards = len(h.get("players", []) or [])
        total = cards * stake
        payout = h.get("prize", round(total * 0.8))
        today_profit += (total - payout)

    return {
        'total_users': total_users,
        'today_deposits': today_deposits,
        'today_withdrawals': today_withdrawals,
        'today_profit': today_profit,
        'today_payout': today_payout,
        'games_today': games_today,
        'pending_deposits': transactions_col.count_documents({"type": "deposit", "status": "pending"}),
        'pending_withdrawals': transactions_col.count_documents({"type": "withdraw", "status": "pending"}),
    }


# ==========================================
# ADMIN: GAME HISTORY
# ==========================================

def get_admin_game_history(limit=100, period=None):
    games = []

    def cards_for(game_id, fallback):
        """Real number of cards SELECTED for this game (not player count)."""
        try:
            count = bingo_cards_col.count_documents({"game_id": game_id})
            return count if count > 0 else fallback
        except Exception:
            return fallback

    # 1) Currently RUNNING games — live snapshot straight from games_col
    for g in games_col.find({"status": "running"}).sort("started_at", -1):
        game_id = g.get("game_id")
        stake = g.get("stake", 10)
        players = cards_for(game_id, g.get("players_count", 0))
        total = players * stake
        payout = round(total * 0.8)
        profit = total - payout
        games.append({
            'game_id': game_id,
            'room': stake,
            'players': players,
            'winner_name': '—',
            'bet': stake,
            'total': total,
            'payout': payout,
            'profit': profit,
            'status': 'Running',
            'date': g.get("started_at") or g.get("created_at") or '',
        })

    # 2) FINISHED games — from the real game_history archive
    query = {}
    if period:
        now = datetime.utcnow()
        if period == 'daily':
            query = {"date": {"$regex": "^" + now.strftime('%Y-%m-%d')}}
        else:
            days = {'weekly': 7, 'monthly': 30, 'yearly': 365}.get(period, 7)
            query = {"date": {"$gte": (now - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')}}
    for h in game_history_col.find(query).sort("date", -1).limit(limit):
        game_id = h.get("game_id")
        stake = h.get("stake", 10)
        winners_list = h.get("winner", []) or []
        fallback_players = len(h.get("players", []) or [])
        players = cards_for(game_id, fallback_players)
        total = players * stake
        payout = h.get("prize", round(total * 0.8))
        profit = total - payout
        games.append({
            'game_id': game_id,
            'room': stake,
            'players': players,
            'winner_name': ', '.join(winners_list) if winners_list else '—',
            'bet': stake,
            'total': total,
            'payout': payout,
            'profit': profit,
            'status': 'Completed',
            'date': h.get("date", ''),
        })

    games.sort(key=lambda g: g['date'], reverse=True)
    return games[:limit]


def get_period_profit(period='daily'):
    """Real house profit for a period, using the SAME calculation as
    Game History and the dashboard: (players * stake) - payout, summed
    across every game that finished in this period."""
    now = datetime.utcnow()
    if period == 'daily':
        since = now.strftime('%Y-%m-%d')
    elif period == 'weekly':
        since = (now - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
    elif period == 'monthly':
        since = (now - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
    else:  # yearly
        since = (now - timedelta(days=365)).strftime('%Y-%m-%d %H:%M:%S')
    query = {"date": {"$regex": f"^{since}"}} if period == 'daily' else {"date": {"$gte": since}}
    total_profit = 0
    for h in game_history_col.find(query):
        game_id = h.get("game_id")
        stake = h.get("stake", 10)
        try:
            cards = bingo_cards_col.count_documents({"game_id": game_id})
        except Exception:
            cards = 0
        if cards == 0:
            cards = len(h.get("players", []) or [])
        total = cards * stake
        payout = h.get("prize", round(total * 0.8))
        total_profit += (total - payout)
    return total_profit


def get_period_revenue(period='daily'):
    """Real total revenue (total stakes collected) for a period — every
    card sold across every game that finished in this period, before the
    80/20 split."""
    now = datetime.utcnow()
    if period == 'daily':
        since = now.strftime('%Y-%m-%d')
    elif period == 'weekly':
        since = (now - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
    elif period == 'monthly':
        since = (now - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
    else:  # yearly
        since = (now - timedelta(days=365)).strftime('%Y-%m-%d %H:%M:%S')
    query = {"date": {"$regex": f"^{since}"}} if period == 'daily' else {"date": {"$gte": since}}
    total_revenue = 0
    for h in game_history_col.find(query):
        game_id = h.get("game_id")
        stake = h.get("stake", 10)
        try:
            cards = bingo_cards_col.count_documents({"game_id": game_id})
        except Exception:
            cards = 0
        if cards == 0:
            cards = len(h.get("players", []) or [])
        total_revenue += cards * stake
    return total_revenue


def get_daily_revenue_last_7_days():
    """Real daily revenue = actual completed DEPOSITS for each of the last
    7 days, oldest to newest — for the Dashboard's 'Daily Revenue' chart."""
    labels = []
    values = []
    for i in range(6, -1, -1):
        day = datetime.utcnow() - timedelta(days=i)
        day_str = day.strftime('%Y-%m-%d')
        label = day.strftime('%a')  # Mon, Tue, Wed...
        pipeline = [
            {"$match": {"type": "deposit", "status": "completed", "time": {"$regex": f"^{day_str}"}}},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
        ]
        res = list(transactions_col.aggregate(pipeline))
        total = res[0]["total"] if res else 0
        labels.append(label)
        values.append(total)
    return {'labels': labels, 'values': values}


def get_user_growth_last_7_days():
    """Real cumulative total users, day by day, for the last 7 days —
    for the Dashboard's 'User Growth' chart."""
    labels = []
    values = []
    for i in range(6, -1, -1):
        day = datetime.utcnow() - timedelta(days=i)
        day_end = day.strftime('%Y-%m-%d') + ' 23:59:59'
        label = day.strftime('%a')  # Mon, Tue, Wed...
        count = users_col.count_documents({"created_at": {"$lte": day_end}})
        labels.append(label)
        values.append(count)
    return {'labels': labels, 'values': values}


def get_hourly_game_activity_today():
    """Real count of games finished today, bucketed into the same 9 time
    slots as the old fake chart — for the Dashboard's 'Game Activity' chart."""
    today = datetime.utcnow().strftime('%Y-%m-%d')
    slots = [
        ('6am', 6, 8), ('8am', 8, 10), ('10am', 10, 12), ('12pm', 12, 14),
        ('2pm', 14, 16), ('4pm', 16, 18), ('6pm', 18, 20), ('8pm', 20, 22), ('10pm', 22, 24)
    ]
    counts = [0] * len(slots)
    for h in game_history_col.find({"date": {"$regex": f"^{today}"}}):
        date_str = h.get("date", "")
        try:
            hour = int(date_str.split(' ')[1].split(':')[0])
        except (IndexError, ValueError):
            continue
        for i, (label, start, end) in enumerate(slots):
            if start <= hour < end:
                counts[i] += 1
                break
    return {'labels': [s[0] for s in slots], 'values': counts}


def get_monthly_revenue_last_6_months():
    """Real total deposits, summed by calendar month, for the last 6
    months — for the Dashboard's 'Monthly Revenue' chart."""
    labels = []
    values = []
    for i in range(5, -1, -1):
        dt = datetime.utcnow() - timedelta(days=i*30)
        month_str = dt.strftime('%Y-%m')
        label = dt.strftime('%b')  # Jan, Feb, Mar...
        pipeline = [
            {"$match": {"type": "deposit", "status": "completed", "time": {"$regex": f"^{month_str}"}}},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
        ]
        res = list(transactions_col.aggregate(pipeline))
        total = res[0]["total"] if res else 0
        labels.append(label)
        values.append(total)
    return {'labels': labels, 'values': values}

# ==========================================
# ADMIN: REPORTS
# ==========================================
def get_admin_reports(period='daily'):
    rows = []
    def get_stats_for_date(d_start, d_end=None):
        match_start = {"$regex": f"^{d_start}"}
        match_range = {"$gte": d_start, "$lt": d_end} if d_end else match_start
        
        match_dep = {"type": "deposit", "status": "completed", "time": match_range}
        match_wit = {"type": "withdraw", "status": "completed", "time": match_range}
        match_win = {"type": "bingo_win", "status": "completed", "time": match_range}
        
        def get_sum(match):
            p = [{"$match": match}, {"$group": {"_id": None, "t": {"$sum": "$amount"}}}]
            res = list(transactions_col.aggregate(p))
            return res[0]["t"] if res else 0
        dep = get_sum(match_dep)
        wit = get_sum(match_wit)
        pay = get_sum(match_win)
        games = game_sessions_col.count_documents({"time": match_range})
        return dep, wit, pay, games
    if period == 'daily':
        for i in range(7, -1, -1):
            d = (datetime.utcnow() - timedelta(days=i)).strftime('%Y-%m-%d')
            dep, wit, pay, games = get_stats_for_date(d)
            rows.append({'date': d, 'deposits': dep, 'withdrawals': wit, 'payout': pay, 'games': games, 'profit': dep - wit - pay})
    elif period == 'weekly':
        for i in range(3, -1, -1):
            ws = (datetime.utcnow() - timedelta(weeks=i))
            ws = ws - timedelta(days=ws.weekday())
            we = ws + timedelta(days=7)
            d_start = ws.strftime('%Y-%m-%d')
            d_end = we.strftime('%Y-%m-%d')
            dep, wit, pay, games = get_stats_for_date(d_start, d_end)
            rows.append({'date': f"{d_start} to {we.strftime('%Y-%m-%d')}", 'deposits': dep, 'withdrawals': wit, 'payout': pay, 'games': games, 'profit': dep - wit - pay})
    elif period == 'monthly':
        for i in range(5, -1, -1):
            dt = datetime.utcnow() - timedelta(days=i*30)
            m = dt.strftime('%Y-%m')
            dep, wit, pay, games = get_stats_for_date(m)
            rows.append({'date': m, 'deposits': dep, 'withdrawals': wit, 'payout': pay, 'games': games, 'profit': dep - wit - pay})
    elif period == 'yearly':
        for i in range(2, -1, -1):
            y = str(datetime.utcnow().year - i)
            dep, wit, pay, games = get_stats_for_date(y)
            rows.append({'date': y, 'deposits': dep, 'withdrawals': wit, 'payout': pay, 'games': games, 'profit': dep - wit - pay})
    return rows


# ==========================================
# AGENT SYSTEM FUNCTIONS
# ==========================================
def get_agent_rankings(period='week', limit=100):
    """Real agent rankings: for every user marked is_agent, sum their
    referred users' deposits within the period, and compute the 10%
    commission using the SAME formula the bot already pays out live
    (see main.py deposit-confirmation flow)."""
    since = get_period_start(period)
    agents = list(users_col.find({"is_agent": 1}))
    result = []
    for a in agents:
        uid = a["user_id"]
        invited_ids = [u["user_id"] for u in users_col.find({"referred_by": uid}, {"user_id": 1})]
        pipeline = [
            {"$match": {"user_id": {"$in": invited_ids}, "type": "deposit", "status": "completed", "time": {"$gte": since}}},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}, "depositors": {"$addToSet": "$user_id"}}}
        ]
        agg = list(transactions_col.aggregate(pipeline))
        volume = agg[0]["total"] if agg else 0
        depositors = len(agg[0]["depositors"]) if agg else 0
        result.append({
            'user_id': uid,
            'first_name': a.get('first_name', 'User'),
            'phone': a.get('phone', '—'),
            'referral_count': len(invited_ids),
            'referral_depositors': depositors,
            'referral_volume': volume,
            'commission_earned': round(volume * 0.10),
            'commission_paid': True,
            'is_agent': True,
        })
    result.sort(key=lambda x: x['commission_earned'], reverse=True)
    return result[:limit]

def is_user_agent(user_id):
    u = users_col.find_one({"user_id": user_id})
    return u and u.get("is_agent") == 1

def check_and_upgrade_agent(user_id):
    if is_user_agent(user_id): return False
    invites = get_referral_count(user_id)
    depositors = get_depositing_referrals_count(user_id)
    total_deps = get_total_referral_deposits(user_id)
    if invites >= 30 and depositors >= 20 and total_deps >= 3000:
        users_col.update_one({"user_id": user_id}, {"$set": {"is_agent": 1}})
        return True
    return False


# ==========================================
# LANGUAGE FUNCTIONS
# ==========================================

def get_user_language(user_id):
    u = users_col.find_one({"user_id": user_id})
    return u.get("language", 'am') if u else 'am'

def set_user_language(user_id, lang):
    users_col.update_one({"user_id": user_id}, {"$set": {"language": lang}})


# ==========================================
# PHONE LOOKUP
# ==========================================

def get_user_by_phone(phone):
    clean = phone.replace(" ", "").replace("+", "").replace("-", "").replace("(", "").replace(")", "")
    variations = [clean]
    if clean.startswith("251"):
        variations.append("0" + clean[3:])
        variations.append("+251" + clean[3:])
    elif clean.startswith("0"):
        variations.append("251" + clean[1:])
        variations.append("+251" + clean[1:])
    elif len(clean) == 9 and not clean.startswith("0"):
        variations.append("0" + clean)
        variations.append("251" + clean)
        variations.append("+251" + clean)
    variations = list(set(variations))
    
    u = users_col.find_one({"phone": {"$in": variations}})
    if not u: return None
    return (u.get("user_id"), u.get("phone"), u.get("main_balance", 0), u.get("play_balance", 0), u.get("referred_by"))


# ==========================================
# MULTIPLAYER: GAMES (Step 6 upgrade)
# ----------------------------------------
# games:
#   game_id, stake, status, players_count, max_players,
#   called_numbers, current_number, prize_pool, winner,
#   created_at, started_at, finished_at
# game_players:
#   game_id, telegram_user_id, card_id, joined_at, status
# bingo_cards:
#   game_id, card_id, owner_id, numbers, marked_numbers
# game_history:
#   game_id, players, winner, prize, date
# ==========================================

def create_game_record(game_id, stake, status='waiting', max_players=100):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    games_col.update_one(
        {"game_id": game_id},
        {"$setOnInsert": {
            "game_id": game_id, "stake": stake, "status": status,
            "players_count": 0, "max_players": max_players,
            "called_numbers": [], "current_number": None, "prize_pool": 0,
            "winner": None, "created_at": now, "started_at": None, "finished_at": None
        }},
        upsert=True
    )

def get_game_record(game_id):
    return games_col.find_one({"game_id": game_id})

def claim_open_room(stake, candidate_game_id):
    """
    Resolve the ONE open (waiting) room's game_id for a stake, atomically,
    across every server process/instance.

    If no room is open yet for this stake, `candidate_game_id` wins and
    becomes the open room. If another process already claimed one first,
    that existing game_id is returned instead and `candidate_game_id` is
    simply discarded. Mongo guarantees only one caller can ever "win" a
    given stake at a time, even with many processes calling this at once.

    This is what stops two different server processes from each silently
    handing out a different Game ID for the same stake tier.
    """
    doc = open_rooms_col.find_one_and_update(
        {"stake": stake},
        {"$setOnInsert": {"stake": stake, "game_id": candidate_game_id}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc["game_id"]

def release_open_room(stake, game_id):
    """Stop treating `game_id` as the open room for `stake` (only if it still is)."""
    open_rooms_col.delete_one({"stake": stake, "game_id": game_id})

def update_game_record(game_id, **fields):
    games_col.update_one({"game_id": game_id}, {"$set": fields})

def delete_game_record(game_id):
    games_col.delete_one({"game_id": game_id})

def add_game_player(game_id, user_id, card_id, stake):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    game_players_col.update_one(
        {"game_id": game_id, "telegram_user_id": user_id},
        {"$setOnInsert": {
            "game_id": game_id, "telegram_user_id": user_id, "card_id": card_id,
            "joined_at": now, "status": "joined"
        }},
        upsert=True
    )
    games_col.update_one({"game_id": game_id}, {"$inc": {"players_count": 1}})

def update_game_player_status(game_id, user_id, status):
    game_players_col.update_one(
        {"game_id": game_id, "telegram_user_id": user_id},
        {"$set": {"status": status}}
    )

def get_game_players(game_id):
    return list(game_players_col.find({"game_id": game_id}))

def add_bingo_card(game_id, card_id, owner_id, numbers):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    bingo_cards_col.insert_one({
        "game_id": game_id, "card_id": card_id, "owner_id": owner_id,
        "numbers": numbers, "marked_numbers": [], "created_at": now
    })

def get_bingo_card(card_id):
    return bingo_cards_col.find_one({"card_id": card_id})

def get_cards_for_player(game_id, user_id):
    return list(bingo_cards_col.find({"game_id": game_id, "owner_id": user_id}))

def update_card_marked(card_id, marked):
    bingo_cards_col.update_one({"card_id": card_id}, {"$set": {"marked_numbers": marked}})

def record_game_history(game_id, players, winners, prize, stake):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    game_history_col.insert_one({
        "game_id": game_id, "players": players, "winner": winners,
        "prize": prize, "stake": stake, "date": now
    })

def get_recent_games(limit=50):
    return list(game_history_col.find().sort("date", -1).limit(limit))
