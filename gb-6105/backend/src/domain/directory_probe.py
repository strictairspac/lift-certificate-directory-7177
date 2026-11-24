"""按指定目录版本执行的一批点查。

同一批次点查在批次开始时固定目录版本，整批复用同一版目录，
切换发生在批次中途也不改用新版，保证同批结论口径一致。
"""
from .hash_directory import Directory


def probe_batch(directory_snapshot, probes, review_date):
    directory = Directory.from_snapshot(directory_snapshot)
    rows = []
    unavailable = 0
    for code in probes:
        bucket, cert = directory.locate(code)
        verdict = cert.verdict(review_date) if cert else "未登记"
        unavailable += verdict != "有效"
        rows.append({
            "类别": "点查请求", "项目": code, "桶号": bucket.number, "局部深度": bucket.depth,
            "桶内条数": len(bucket.records), "电梯或槽位": cert.lift if cert else None,
            "结论": verdict,
        })
    for bucket in directory.buckets():
        spill = max(0, len(bucket.records) - directory.capacity)
        rows.append({
            "类别": "目录追踪", "项目": f"B{bucket.number:03d}", "桶号": bucket.number,
            "局部深度": bucket.depth, "桶内条数": len(bucket.records),
            "电梯或槽位": f"{1 << (directory.depth - bucket.depth)} 个目录槽位",
            "结论": f"深度封顶，溢出 {spill} 条" if spill else "容量内",
        })
    return {
        "rows": rows,
        "metrics": {"目录槽位": len(directory.slots), "桶分裂": directory.splits, "异常检证": unavailable},
        "unavailable": unavailable,
    }
