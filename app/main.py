"""Employee Attendance & Analytics API (HROne assignment, contract v2.0)."""
import os, re, math
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError, ServerSelectionTimeoutError, AutoReconnect

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=2000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30)); UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}; STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def ensure_indexes():
    try:
        _ensure_indexes_connected()
    except (ServerSelectionTimeoutError, AutoReconnect):
        # Let the app start so /health can correctly report 503 while MongoDB is down.
        return

def _ensure_indexes_connected():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_date_status")
    db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING), ("late_minutes", DESCENDING)], name="ix_attendance_month_late")

# ---- boundary and business-rule helpers ----
def parse_date(s: str) -> date:
    try:
        d = date.fromisoformat(s)
        if d.isoformat() != s: raise ValueError()
        return d
    except Exception: raise HTTPException(422, "date must be YYYY-MM-DD")

def parse_month(s: str) -> tuple[date,date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", s or ""): raise HTTPException(422, "month must be YYYY-MM")
    y,m = map(int,s.split('-')); first=date(y,m,1)
    return first, date(y+1,1,1) if m==12 else date(y,m+1,1)

def epoch_instant(ms: int | None) -> datetime:
    if ms is None: return datetime.now(UTC).replace(microsecond=0)
    if type(ms) is not int or ms < 100_000_000_000 or ms > 4_102_444_800_000: raise HTTPException(422,"timestamp must be epoch milliseconds")
    try: return datetime.fromtimestamp(ms/1000, UTC).replace(microsecond=0)
    except (ValueError, OverflowError, OSError): raise HTTPException(422,"invalid epoch milliseconds")

def as_utc(dt):
    if dt is None: return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)

def ms(dt):
    if dt is None: return None
    return int(as_utc(dt).timestamp()*1000)

def money_round(x, places=2):
    if x is None: return None
    q=Decimal("1") if places==0 else Decimal("1").scaleb(-places)
    return float(Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP))

def shift_bounds(punch_in: datetime, shift_start: str, shift_end: str):
    local=as_utc(punch_in).astimezone(IST); day=local.date(); tod=local.strftime('%H:%M')
    overnight=shift_end <= shift_start
    if overnight and tod < shift_end: day -= timedelta(days=1)
    sh,smn=map(int,shift_start.split(':')); eh,emn=map(int,shift_end.split(':'))
    start=datetime.combine(day,time(sh,smn),IST)
    end_day=day+timedelta(days=1) if overnight else day
    end=datetime.combine(end_day,time(eh,emn),IST)
    return day,start,end

def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str="18:30") -> int:
    _, start, _=shift_bounds(punch_in,shift_start,shift_end)
    sec=int((as_utc(punch_in)-start.astimezone(UTC)).total_seconds())
    return (sec//60) if sec > 600 else 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds=int((as_utc(punch_out)-as_utc(punch_in)).total_seconds())
    return money_round(Decimal(seconds)/Decimal(3600),2)

def compute_overtime(punch_out: datetime, shift_end: str, attendance_date: str, shift_start: str="09:30") -> int:
    day=parse_date(attendance_date); eh,em=map(int,shift_end.split(':')); overnight=shift_end<=shift_start
    end=datetime.combine(day+(timedelta(days=1) if overnight else timedelta()),time(eh,em),IST).astimezone(UTC)
    mins=int((as_utc(punch_out)-end).total_seconds()//60)
    return mins if mins>=30 else 0

def attendance_day(punch: datetime, employee: dict) -> date:
    d,_,_=shift_bounds(punch,employee['shift_start'],employee['shift_end']); return d

def weekdays(start: date, end_exclusive: date) -> int:
    if start>=end_exclusive:return 0
    n=(end_exclusive-start).days; weeks,rem=divmod(n,7); total=weeks*5
    for i in range(rem):
        if (start+timedelta(days=weeks*7+i)).weekday()<5: total+=1
    return total

def output_attendance(doc):
    return {"emp_code":doc["emp_code"],"date":doc["date"],"status":doc["status"],"punch_in":ms(doc.get("punch_in")),"punch_out":ms(doc.get("punch_out")),"work_hours":doc.get("work_hours"),"late_minutes":doc.get("late_minutes",0),"overtime_minutes":doc.get("overtime_minutes",0),"half_day":doc.get("half_day",False),"history":[{"at":ms(h.get('at')),"by":h.get('by'),"reason":h.get('reason'),"changes":{k:{"from":ms(v.get('from')) if k in ('punch_in','punch_out') else v.get('from'),"to":ms(v.get('to')) if k in ('punch_in','punch_out') else v.get('to')} for k,v in h.get('changes',{}).items()}} for h in doc.get('history',[])]}
def output_employee(doc):
    return {k:(ms(v) if k=='created_at' else v) for k,v in doc.items() if k!='_id'}
def valid_page(page,page_size):
    if page<1: raise HTTPException(422,"page must be >= 1")
    if page_size<1 or page_size>100: raise HTTPException(422,"page_size must be between 1 and 100")

# ---- request models ----
class EmployeeIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    emp_code:str=Field(pattern=r'^EMP\d{4,6}$')
    name:str=Field(min_length=1,max_length=100)
    email:str=Field(max_length=120,pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department:str=Field(min_length=1,max_length=50)
    shift_start:str=Field(default='09:30',pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    shift_end:str=Field(default='18:30',pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    joined_on:str
    @field_validator('joined_on')
    @classmethod
    def valid_join_date(cls,v):
        try:
            if date.fromisoformat(v).isoformat()!=v: raise ValueError()
        except Exception: raise ValueError('joined_on must be YYYY-MM-DD')
        return v
    @model_validator(mode='after')
    def shift_differs(self):
        if self.shift_start==self.shift_end: raise ValueError('shift_start must differ from shift_end')
        return self
class PunchInIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    emp_code:str
    punched_at:Optional[int]=Field(default=None,ge=100_000_000_000,le=4_102_444_800_000)
    status:Literal['PRESENT','WFH','ON_DUTY']='PRESENT'
    @field_validator('punched_at',mode='before')
    @classmethod
    def int_ms(cls,v):
        if v is None or type(v) is not int: raise ValueError('punched_at must be integer epoch milliseconds')
        return v
class PunchOutIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    emp_code:str
    punched_at:Optional[int]=Field(default=None,ge=100_000_000_000,le=4_102_444_800_000)
    @field_validator('punched_at',mode='before')
    @classmethod
    def int_ms(cls,v):
        if v is None or type(v) is not int: raise ValueError('punched_at must be integer epoch milliseconds')
        return v
class RegularizeIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    status:Optional[Literal['PRESENT','ABSENT','LEAVE','WFH','ON_DUTY']]=None
    punch_in:Optional[int]=Field(default=None,ge=100_000_000_000,le=4_102_444_800_000)
    punch_out:Optional[int]=Field(default=None,ge=100_000_000_000,le=4_102_444_800_000)
    reason:str=Field(min_length=5,max_length=200)
    regularized_by:str=Field(min_length=1,max_length=50)
    @field_validator('punch_in','punch_out',mode='before')
    @classmethod
    def validate_ms(cls,v):
        if v is None or type(v) is not int: raise ValueError('timestamps must be integer epoch milliseconds')
        return v

# ---- CRUD ----
@app.get('/health')
def health():
    try: db.command('ping'); return {'status':'ok'}
    except PyMongoError: raise HTTPException(503,'MongoDB unavailable')

@app.post('/employees',status_code=201)
def create_employee(body:EmployeeIn):
    doc=body.model_dump(); doc['created_at']=datetime.now(UTC).replace(microsecond=0)
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,'emp_code already exists')
    return output_employee(doc)

@app.get('/employees')
def list_employees(department:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={'department':department} if department is not None else {}
    cur=db.employees.find(q,{'_id':0}).sort('emp_code',ASCENDING).skip((page-1)*page_size).limit(page_size)
    return {'items':[output_employee(x) for x in cur],'total':db.employees.count_documents(q),'page':page,'page_size':page_size}

@app.post('/attendance/punch-in',status_code=201)
def punch_in(body:PunchInIn):
    emp=db.employees.find_one({'emp_code':body.emp_code})
    if not emp: raise HTTPException(404,'employee not found')
    ts=epoch_instant(body.punched_at); d=attendance_day(ts,emp).isoformat()
    doc={'emp_code':body.emp_code,'date':d,'status':body.status,'punch_in':ts,'punch_out':None,'work_hours':None,'late_minutes':compute_late_minutes(ts,emp['shift_start'],emp['shift_end']),'overtime_minutes':0,'half_day':False,'history':[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,'already punched in for this date')
    return output_attendance(doc)

@app.post('/attendance/punch-out')
def punch_out(body:PunchOutIn):
    emp=db.employees.find_one({'emp_code':body.emp_code})
    if not emp: raise HTTPException(404,'employee not found')
    ts=epoch_instant(body.punched_at)
    # The most recent open punch at or before this instant, including an overnight shift.
    rec=db.attendance_logs.find_one({'emp_code':body.emp_code,'punch_in':{'$ne':None,'$lte':ts}},sort=[('punch_in',DESCENDING)])
    if not rec: raise HTTPException(404,'no punch-in found')
    if rec.get('punch_out') is not None: raise HTTPException(409,'already punched out')
    secs=(ts-as_utc(rec['punch_in'])).total_seconds()
    if secs<=0 or secs>86400: raise HTTPException(422,'punch-out must be after punch-in and within 24 hours')
    hours=compute_work_hours(rec['punch_in'],ts); overtime=compute_overtime(ts,emp['shift_end'],rec['date'],emp['shift_start'])
    updated=db.attendance_logs.find_one_and_update({'_id':rec['_id'],'punch_out':None},{'$set':{'punch_out':ts,'work_hours':hours,'overtime_minutes':overtime,'half_day':hours<4.5}},return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409,'already punched out')
    return output_attendance(updated)

@app.get('/attendance')
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[Literal['PRESENT','ABSENT','LEAVE','WFH','ON_DUTY']]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    if date_from: parse_date(date_from)
    if date_to: parse_date(date_to)
    if date_from and date_to and date_from>date_to: raise HTTPException(422,'date_from must be <= date_to')
    q={}
    if emp_code:q['emp_code']=emp_code
    if status:q['status']=status
    if date_from or date_to:
        q['date']={}
        if date_from:q['date']['$gte']=date_from
        if date_to:q['date']['$lte']=date_to
    docs=db.attendance_logs.find(q).sort([('date',DESCENDING),('emp_code',ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {'items':[output_attendance(x) for x in docs],'total':db.attendance_logs.count_documents(q),'page':page,'page_size':page_size}

@app.patch('/attendance/{emp_code}/{date}')
def regularize(emp_code:str,date:str,body:RegularizeIn):
    parse_date(date)
    emp=db.employees.find_one({'emp_code':emp_code})
    if not emp: raise HTTPException(404,'employee not found')
    old=db.attendance_logs.find_one({'emp_code':emp_code,'date':date})
    if not old: raise HTTPException(404,'attendance record not found')
    status=body.status if body.status is not None else old['status']
    if status in {'ABSENT','LEAVE'}:
        if body.punch_in is not None or body.punch_out is not None: raise HTTPException(422,'ABSENT/LEAVE cannot have punch times')
        pi=po=None
    else:
        pi=epoch_instant(body.punch_in) if body.punch_in is not None else as_utc(old.get('punch_in'))
        po=epoch_instant(body.punch_out) if body.punch_out is not None else as_utc(old.get('punch_out'))
        if pi is None: raise HTTPException(422,'presence status requires punch_in')
        if attendance_day(pi,emp).isoformat()!=date: raise HTTPException(422,'punch_in must fall on attendance date')
        if po is not None and ((po-pi).total_seconds()<=0 or (po-pi).total_seconds()>86400): raise HTTPException(422,'punch_out must be after punch_in and within 24 hours')
    late=compute_late_minutes(pi,emp['shift_start'],emp['shift_end']) if pi else 0
    hours=compute_work_hours(pi,po) if pi and po else None
    overtime=compute_overtime(po,emp['shift_end'],date,emp['shift_start']) if pi and po else 0
    half=bool(hours is not None and hours<4.5)
    final={'status':status,'punch_in':pi,'punch_out':po,'work_hours':hours,'late_minutes':late,'overtime_minutes':overtime,'half_day':half}
    changes={}
    for k,v in final.items():
        ov=old.get(k,False if k=='half_day' else 0 if k in ('late_minutes','overtime_minutes') else None)
        if k in ('punch_in','punch_out'): ov=as_utc(ov) if ov else None
        if ov!=v: changes[k]={'from':ov,'to':v}
    if not changes: raise HTTPException(422,'request does not change the record')
    entry={'at':datetime.now(UTC).replace(microsecond=0),'by':body.regularized_by,'reason':body.reason,'changes':changes}
    # Optimistic concurrency: matching the previous history and mutable values ensures a concurrent edit cannot overwrite history.
    query={'_id':old['_id'],'status':old.get('status')}
    query['history'] = old['history'] if 'history' in old else {'$exists': False}
    for k in ('punch_in','punch_out','work_hours','late_minutes','overtime_minutes','half_day'):
        if k in old: query[k]=old[k]
        else: query[k]={'$exists':False}
    updated=db.attendance_logs.find_one_and_update(query,{'$set':final,'$push':{'history':entry}},return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409,'record changed concurrently; retry correction')
    return output_attendance(updated)

# ---- analytics aggregation helpers ----
def month_bounds_strings(month):
    start,end=parse_month(month); return start.isoformat(),end.isoformat(),start,end


@app.get('/analytics/employees/{emp_code}/monthly')
def employee_monthly(emp_code:str,month:str=Query(...,pattern=r'^\d{4}-(0[1-9]|1[0-2])$')):
    start_s,end_s,start,end=month_bounds_strings(month)
    emp=db.employees.find_one({'emp_code':emp_code})
    if not emp: raise HTTPException(404,'employee not found')
    effective=max(start,date.fromisoformat(emp['joined_on']))
    working=weekdays(effective,end) if effective<end else 0
    pipe=[{'$match':{'emp_code':emp_code}},{'$lookup':{'from':'attendance_logs','let':{'code':'$emp_code'},'pipeline':[{'$match':{'$expr':{'$and':[{'$eq':['$emp_code','$$code']},{'$gte':['$date',start_s]},{'$lt':['$date',end_s]}]}}}], 'as':'logs'}},{'$unwind':{'path':'$logs','preserveNullAndEmptyArrays':True}},{'$group':{'_id':'$emp_code','present_days':{'$sum':{'$cond':[{'$and':[{'$in':['$logs.status',['PRESENT','WFH','ON_DUTY']]},{'$gte':['$logs.date',effective.isoformat()]},{'$lte':[{'$dayOfWeek':{'$dateFromString':{'dateString':{'$ifNull':['$logs.date','2000-01-01']}}}},6]},{'$gte':[{'$dayOfWeek':{'$dateFromString':{'dateString':{'$ifNull':['$logs.date','2000-01-01']}}}},2]}]},{'$cond':[{'$eq':['$logs.half_day',True]},0.5,1]},0]}},'leave_days':{'$sum':{'$cond':[{'$eq':['$logs.status','LEAVE']},1,0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$logs.late_minutes',0]},0]},1,0]}},'total_late_minutes':{'$sum':{'$ifNull':['$logs.late_minutes',0]}},'total_overtime_minutes':{'$sum':{'$ifNull':['$logs.overtime_minutes',0]}}}}]
    rows=list(db.employees.aggregate(pipe)); r=rows[0] if rows else {'present_days':0,'leave_days':0,'late_count':0,'total_late_minutes':0,'total_overtime_minutes':0}
    present=money_round(r.get('present_days',0),2); pct=money_round(present/working*100,2) if working else None
    return {'emp_code':emp_code,'month':month,'working_days':working,'present_days':present,'leave_days':r.get('leave_days',0),'late_count':r.get('late_count',0),'total_late_minutes':r.get('total_late_minutes',0),'total_overtime_minutes':r.get('total_overtime_minutes',0),'attendance_pct':pct}

@app.get('/analytics/departments/summary')
def department_summary(month:str=Query(...,pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),department:Optional[str]=None):
    start_s,end_s,start,end=month_bounds_strings(month)
    match={'joined_on':{'$lt':end.isoformat()}}
    if department is not None:match['department']=department
    pipe=[{'$match':match},{'$lookup':{'from':'attendance_logs','let':{'code':'$emp_code'},'pipeline':[{'$match':{'$expr':{'$and':[{'$eq':['$emp_code','$$code']},{'$gte':['$date',start_s]},{'$lt':['$date',end_s]}]}}}], 'as':'logs'}},{'$unwind':{'path':'$logs','preserveNullAndEmptyArrays':True}},{'$group':{'_id':'$department','headcount':{'$addToSet':'$emp_code'},'present_days':{'$sum':{'$cond':[{'$and':[{'$in':['$logs.status',['PRESENT','WFH','ON_DUTY']]},{'$gte':[{'$dayOfWeek':{'$dateFromString':{'dateString':{'$ifNull':['$logs.date','2000-01-01']}}}},2]},{'$lte':[{'$dayOfWeek':{'$dateFromString':{'dateString':{'$ifNull':['$logs.date','2000-01-01']}}}},6]}]},{'$cond':[{'$eq':['$logs.half_day',True]},0.5,1]},0]}},'work_hours_sum':{'$sum':{'$cond':[{'$and':[{'$in':['$logs.status',['PRESENT','WFH','ON_DUTY']]},{'$ne':[{'$ifNull':['$logs.work_hours',None]},None]}]},'$logs.work_hours',0]}},'work_hours_n':{'$sum':{'$cond':[{'$and':[{'$in':['$logs.status',['PRESENT','WFH','ON_DUTY']]},{'$ne':[{'$ifNull':['$logs.work_hours',None]},None]}]},1,0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$logs.late_minutes',0]},0]},1,0]}},'total_late_minutes':{'$sum':{'$ifNull':['$logs.late_minutes',0]}},'leave_count':{'$sum':{'$cond':[{'$eq':['$logs.status','LEAVE']},1,0]}},'on_duty_count':{'$sum':{'$cond':[{'$eq':['$logs.status','ON_DUTY']},1,0]}}}},{'$project':{'_id':0,'department':'$_id','headcount':{'$size':'$headcount'},'present_days':1,'avg_work_hours':{'$cond':[{'$gt':['$work_hours_n',0]},{'$divide':['$work_hours_sum','$work_hours_n']},None]},'late_count':1,'total_late_minutes':1,'leave_count':1,'on_duty_count':1}},{'$sort':{'department':1}}]
    rows=list(db.employees.aggregate(pipe))
    for r in rows:
        if r['avg_work_hours'] is not None:r['avg_work_hours']=money_round(r['avg_work_hours'],2)
        r['present_days']=money_round(r['present_days'],2)
    return {'month':month,'items':rows}

@app.get('/analytics/leaderboard/late')
def late_leaderboard(month:str=Query(...,pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    start_s,end_s,_,_=month_bounds_strings(month)
    pipe=[{'$match':{'date':{'$gte':start_s,'$lt':end_s},'late_minutes':{'$gt':0}}},{'$group':{'_id':'$emp_code','total_late_minutes':{'$sum':'$late_minutes'},'late_count':{'$sum':1}}},{'$match':{'total_late_minutes':{'$gt':0}}},{'$lookup':{'from':'employees','localField':'_id','foreignField':'emp_code','as':'employee'}},{'$unwind':'$employee'}]
    if department is not None: pipe.append({'$match':{'employee.department':department}})
    pipe += [{'$setWindowFields':{'sortBy':{'total_late_minutes':-1},'output':{'rank':{'$rank':{}}}}},{'$match':{'rank':{'$lte':limit}}},{'$sort':{'total_late_minutes':-1,'_id':1}},{'$project':{'_id':0,'rank':1,'emp_code':'$_id','name':'$employee.name','department':'$employee.department','total_late_minutes':1,'late_count':1}}]
    return {'month':month,'items':list(db.attendance_logs.aggregate(pipe,allowDiskUse=True))}

@app.get('/analytics/departments/{department}/trend')
def department_trend(department: str, from_: str = Query(..., alias='from'), to: str = Query(...)):
    dfrom = parse_date(from_); dto = parse_date(to)
    if dto < dfrom: raise HTTPException(422, 'to must be >= from')
    if (dto-dfrom).days + 1 > 92: raise HTTPException(422, 'range cannot exceed 92 days')
    if not db.employees.find_one({'department': department}): raise HTTPException(404, 'department not found')
    # The date spine and moving window are generated inside MongoDB, not in Python.
    pipe = [
      {'$documents': [{'start': datetime.combine(dfrom, time.min, UTC), 'end': datetime.combine(dto, time.min, UTC), 'department': department}]},
      {'$project': {'department': 1, 'dates': {'$map': {
        'input': {'$range': [0, {'$add': [{'$dateDiff': {'startDate': '$start', 'endDate': '$end', 'unit': 'day'}}, 1]}]},
        'as': 'i', 'in': {'$dateAdd': {'startDate': '$start', 'unit': 'day', 'amount': '$$i'}}}}}},
      {'$unwind': '$dates'},
      {'$set': {'date': {'$dateToString': {'date': '$dates', 'format': '%Y-%m-%d'}}}},
      {'$lookup': {'from': 'attendance_logs', 'let': {'d': '$date', 'dept': '$department'}, 'pipeline': [
        {'$match': {'$expr': {'$eq': ['$date', '$$d']}}},
        {'$lookup': {'from': 'employees', 'localField': 'emp_code', 'foreignField': 'emp_code', 'as': 'emp'}},
        {'$unwind': '$emp'}, {'$match': {'$expr': {'$eq': ['$emp.department', '$$dept']}}}
      ], 'as': 'logs'}},
      {'$lookup': {'from': 'employees', 'let': {'dept': '$department', 'd': '$date'}, 'pipeline': [
        {'$match': {'$expr': {'$and': [{'$eq': ['$department', '$$dept']}, {'$lte': ['$joined_on', '$$d']}]}}},
        {'$count': 'n'}
      ], 'as': 'hc'}},
      {'$set': {
        'headcount': {'$ifNull': [{'$arrayElemAt': ['$hc.n', 0]}, 0]},
        'present_count': {'$sum': {'$map': {
          'input': {'$filter': {'input': '$logs', 'as': 'l', 'cond': {'$in': ['$$l.status', ['PRESENT', 'WFH', 'ON_DUTY']]}}},
          'as': 'l', 'in': {'$cond': [{'$eq': ['$$l.half_day', True]}, 0.5, 1]}}}},
        'late_count': {'$size': {'$filter': {'input': '$logs', 'as': 'l', 'cond': {'$gt': [{'$ifNull': ['$$l.late_minutes', 0]}, 0]}}}},
        'dow': {'$dayOfWeek': '$dates'}
      }},
      {'$set': {
        'is_working_day': {'$and': [{'$gte': ['$dow', 2]}, {'$lte': ['$dow', 6]}]},
        'attendance_rate': {'$cond': [
          {'$and': [{'$gte': ['$dow', 2]}, {'$lte': ['$dow', 6]}, {'$gt': ['$headcount', 0]}]},
          {'$divide': [{'$floor': {'$add': [{'$multiply': [{'$divide': ['$present_count', '$headcount']}, 10000]}, 0.5]}}, 10000]}, None
        ]}
      }},
      {'$setWindowFields': {'sortBy': {'dates': 1}, 'output': {'moving_avg_7d': {'$avg': '$attendance_rate', 'window': {'documents': [-6, 0]}}}}},
      {'$project': {'_id': 0, 'date': 1, 'is_working_day': 1, 'headcount': 1,
                    'present_count': '$present_count', 'late_count': 1,
                    'attendance_rate': 1, 'moving_avg_7d': {'$cond': [{'$ne': ['$moving_avg_7d', None]}, {'$divide': [{'$floor': {'$add': [{'$multiply': ['$moving_avg_7d', 10000]}, 0.5]}}, 10000]}, None]}}},
      {'$sort': {'date': 1}}
    ]
    try:
        rows = list(db.command({'aggregate': 1, 'pipeline': pipe, 'cursor': {}, 'allowDiskUse': True})['cursor']['firstBatch'])
    except PyMongoError:
        raise HTTPException(500, 'trend aggregation failed')
    for r in rows:
        if r.get('attendance_rate') is not None: r['attendance_rate'] = money_round(r['attendance_rate'], 4)
        if r.get('moving_avg_7d') is not None: r['moving_avg_7d'] = money_round(r['moving_avg_7d'], 4)
    return {'department': department, 'items': rows}

# ---- explain plans ----
@app.get('/admin/explain/{endpoint}')
def explain_endpoint(endpoint:str,emp_code:Optional[str]=None,month:Optional[str]=None,department:Optional[str]=None,limit:int=Query(10,ge=1,le=50),date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,from_:Optional[str]=Query(None,alias='from'),to:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    allowed={'attendance_list','employee_monthly','department_summary','late_leaderboard','department_trend'}
    if endpoint not in allowed: raise HTTPException(422,'unsupported endpoint')
    if endpoint=='attendance_list':
        if date_from:parse_date(date_from)
        if date_to:parse_date(date_to)
        if date_from and date_to and date_from>date_to:raise HTTPException(422,'date_from must be <= date_to')
        q={}
        if emp_code:q['emp_code']=emp_code
        if status:q['status']=status
        if date_from or date_to:
            q['date']={}
            if date_from:q['date']['$gte']=date_from
            if date_to:q['date']['$lte']=date_to
        cmd={'find':'attendance_logs','filter':q,'sort':{'date':-1,'emp_code':1},'skip':(page-1)*page_size,'limit':page_size}
        coll='attendance_logs'
    else:
        if not month and endpoint!='department_trend':raise HTTPException(422,'month is required')
        if month: start_s,end_s,_,_=month_bounds_strings(month)
        coll='attendance_logs'
        if endpoint=='employee_monthly':
            if not emp_code:raise HTTPException(422,'emp_code is required')
            pipe=[{'$match':{'emp_code':emp_code}},{'$lookup':{'from':'attendance_logs','let':{'code':'$emp_code'},'pipeline':[{'$match':{'$expr':{'$and':[{'$eq':['$emp_code','$$code']},{'$gte':['$date',start_s]},{'$lt':['$date',end_s]}]}}}], 'as':'logs'}}]
            coll='employees'; cmd={'aggregate':'employees','pipeline':pipe,'cursor':{}}
        elif endpoint=='department_summary':
            pipe=[{'$match':{'joined_on':{'$lt':end_s}}},{'$lookup':{'from':'attendance_logs','let':{'code':'$emp_code'},'pipeline':[{'$match':{'$expr':{'$and':[{'$eq':['$emp_code','$$code']},{'$gte':['$date',start_s]},{'$lt':['$date',end_s]}]}}}], 'as':'logs'}}]
            if department:pipe[0]['$match']['department']=department
            coll='employees';cmd={'aggregate':'employees','pipeline':pipe,'cursor':{}}
        elif endpoint=='late_leaderboard':
            pipe=[{'$match':{'date':{'$gte':start_s,'$lt':end_s},'late_minutes':{'$gt':0}}},{'$group':{'_id':'$emp_code','total_late_minutes':{'$sum':'$late_minutes'}}}]
            coll='attendance_logs';cmd={'aggregate':'attendance_logs','pipeline':pipe,'cursor':{}}
        else:
            if not department or not from_ or not to:raise HTTPException(422,'department, from and to are required')
            a=parse_date(from_);b=parse_date(to)
            if b<a or (b-a).days+1>92:raise HTTPException(422,'invalid trend date range')
            pipe=[{'$match':{'date':{'$gte':a.isoformat(),'$lte':b.isoformat()}}},{'$lookup':{'from':'employees','localField':'emp_code','foreignField':'emp_code','as':'emp'}},{'$match':{'emp.department':department}}]
            coll='attendance_logs';cmd={'aggregate':'attendance_logs','pipeline':pipe,'cursor':{}}
    try: ex=db.command({'explain':cmd,'verbosity':'executionStats'})
    except PyMongoError as e: raise HTTPException(500,'explain failed: '+str(e))
    return {'endpoint':endpoint,'collection':coll,'explain':ex}
