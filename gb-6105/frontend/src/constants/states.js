export const States = Object.freeze({
  queued: "等待执行", done: "已完成", failed: "执行失败",
});

export const DirectoryStates = Object.freeze({
  serving: "在用目录", rebuilding: "影子重建中", archived: "已封存只读", failed: "重建未通过",
});

export const ChangeKinds = Object.freeze({
  register: "补录", correct: "更正", revoke: "撤销",
});

export const Actions = Object.freeze({
  create_case: "登记资料", enqueue: "提交执行", done: "执行完成", failed: "执行失败",
  review: "复核签署",
  directory_register: "目录补录", directory_correct: "目录更正", directory_revoke: "目录撤销",
  directory_rebuild_request: "申请缩容重建", directory_publish: "目录切换发布",
  directory_rebuild_failed: "重建核对失败", directory_probe: "固定版本点查",
});
