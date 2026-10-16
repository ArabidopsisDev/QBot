"""Offline synthetic benchmark; does not open QQ or touch live learning data."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import random
import statistics
import tempfile
import time

from qbot import Bot, TEST_GROUP


def main():
    latencies=[]
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'synthetic.sqlite3'
        bot=Bot(str(path),self_id='offline',rng=random.Random(42))
        serial=0
        for i in range(2500):
            question=f'设备编号{i}的运行状态已经'+('好了' if i%2 else '坏了')
            answer=f'设备编号{i}'+('现在可以继续使用' if i%2 else '需要先检查电源线')
            for text in (question,answer):
                serial+=1
                event=dict(post_type='message',message_type='group',group_id=TEST_GROUP,
                    self_id='offline',user_id='human',message_id=str(serial),time=serial,
                    message=[{'type':'text','data':{'text':text}}])
                start=time.perf_counter()
                bot.handle(event)
                latencies.append((time.perf_counter()-start)*1000)
        reply_times=[]
        paraphrase_times=[]
        for i in range(40):
            start=time.perf_counter()
            bot.intelligence.reply(f'设备编号{2400+i}的运行状态已经'+('好了' if i%2 else '坏了'))
            reply_times.append((time.perf_counter()-start)*1000)
            start=time.perf_counter()
            bot.intelligence.reply(f'设备编号{2400+i}的运行状态又'+('好了' if i%2 else '坏了'))
            paraphrase_times.append((time.perf_counter()-start)*1000)
        counts={table:bot.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                for table in ('intel_memory','intel_terms','intel_links')}
        bot.close()
        report={'synthetic_messages':serial,'training_all_three_ms_median':statistics.median(latencies),
            'training_all_three_ms_p95':sorted(latencies)[int(.95*len(latencies))],
            'intelligence_reply_ms_median':statistics.median(reply_times),
            'intelligence_reply_ms_p95':sorted(reply_times)[int(.95*len(reply_times))],
            'paraphrase_reply_ms_median':statistics.median(paraphrase_times),
            'paraphrase_reply_ms_p95':sorted(paraphrase_times)[int(.95*len(paraphrase_times))],
            'database_all_three_models_mb':path.stat().st_size/1024**2,'rows':counts,
            'note':'Synthetic benchmark on this Windows PC; QQ/NapCat memory is excluded; server performance may differ.'}
    class MemoryInfo(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[
            (name,ctypes.c_size_t) for name in ('PeakWorkingSetSize','WorkingSetSize',
            'QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage',
            'QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
    info=MemoryInfo()
    info.cb=ctypes.sizeof(info)
    ctypes.windll.kernel32.GetCurrentProcess.restype=wintypes.HANDLE
    get_memory=ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory.argtypes=[wintypes.HANDLE,ctypes.POINTER(MemoryInfo),wintypes.DWORD]
    if get_memory(ctypes.windll.kernel32.GetCurrentProcess(),ctypes.byref(info),info.cb):
        report['python_process_peak_working_set_mb']=info.PeakWorkingSetSize/1024**2
    output=Path(__file__).parent/'data'/'intelligence-benchmark.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
