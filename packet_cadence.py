"""packet_cadence.py - read-only report of how often each F1 26 packet type arrives.

Usage:   python3 packet_cadence.py sessions/<date>/<time>/raw.bin

It reads raw.bin (record = 8-byte receive time + 4-byte length + packet),
changes nothing, and prints per packet type: count, packets per second,
median gap in frames between packets, and the largest frame gap.
Use it to decide which streams must match by frame ID and which can
carry a "latest value" with an age.
"""
import struct, sys, collections, statistics
HEADER = struct.Struct("<HBBBBBQfIIBB")
names = {0:"Motion",1:"Session",2:"Lap Data",3:"Event",4:"Participants",5:"Car Setups",6:"Car Telemetry",7:"Car Status",8:"Final Class.",9:"Lobby",10:"Car Damage",11:"Session History",12:"Tyre Sets",13:"Motion Ex",14:"Time Trial",15:"Lap Positions",16:"Car Telemetry 2"}
path = sys.argv[1]
cnt = collections.Counter(); sizes = collections.defaultdict(collections.Counter)
first = {}; last = {}
frames = collections.defaultdict(list)   # packet_id -> frame ids (in arrival order)
sess = collections.Counter(); fmts = collections.Counter(); vers=collections.Counter()
t0=None; t1=None; n=0; truncated=False
with open(path,'rb') as f:
    while True:
        p = f.read(12)
        if not p: break
        if len(p)!=12: truncated=True; break
        rns, ln = struct.unpack("<QI", p)
        pk = f.read(ln)
        if len(pk)!=ln: truncated=True; break
        n+=1
        if ln < HEADER.size: continue
        h = HEADER.unpack_from(pk)
        fmt, ver, pid, uid, st, fr, ofr, pidx = h[0], h[4], h[5], h[6], h[7], h[8], h[9], h[10]
        fmts[fmt]+=1; vers[ver]+=1; sess[uid]+=1
        cnt[pid]+=1; sizes[pid][ln]+=1
        if pid not in first: first[pid]=(rns,st)
        last[pid]=(rns,st)
        frames[pid].append((fr, rns))
        t0 = rns if t0 is None else t0; t1 = rns
print("records:", n, "truncated tail:", truncated, "formats:", dict(fmts), "versions:", dict(vers))
print("session UIDs:", len(sess), {hex(k):v for k,v in sess.items()})
dur=(t1-t0)/1e9
print("wall-clock duration: %.1f s (%.1f min)"%(dur,dur/60))
print()
print("%-3s %-16s %8s %9s %10s %12s %s"%("id","name","count","rate/s","size","median dt ms","frame-id gaps (median / max / dup%)"))
for pid in sorted(cnt):
    arr = frames[pid]
    dts = [ (arr[i][1]-arr[i-1][1])/1e6 for i in range(1,len(arr)) ]
    fgaps = [arr[i][0]-arr[i-1][0] for i in range(1,len(arr))]
    dup = sum(1 for g in fgaps if g==0)/max(1,len(fgaps))*100
    span=(last[pid][0]-first[pid][0])/1e9
    rate = cnt[pid]/span if span>0 else float('nan')
    md = statistics.median(dts) if dts else float('nan')
    mg = statistics.median(fgaps) if fgaps else float('nan')
    print("%-3d %-16s %8d %9.2f %10s %12.1f %s / %s / %.1f%%"%(pid,names.get(pid,'?'),cnt[pid],rate,list(sizes[pid])[0],md,mg,max(fgaps) if fgaps else '-',dup))
