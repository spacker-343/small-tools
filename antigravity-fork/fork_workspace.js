// 独立目录派生的参考片段，当前 fork_toolbar.py 安装器不加载此文件。
// 依赖 Antigravity 前端 bundle 中的内部符号，不能作为独立脚本运行；
// 如需接入，必须先核对目标版本的符号映射并验证目录隔离与派生行为。
let desktopWorkspaceForkPending = false;

function desktopForkFolderKey(uri) {
  const url = new URL(uri);
  if (url.protocol !== "file:") return null;
  return (url.hostname + decodeURIComponent(url.pathname))
    .replace(/[\\/]+/g, "/")
    .replace(/\/+$/, "")
    .toLowerCase();
}

function useDesktopWorkspaceFork() {
  const client = Rt();
  const createProject = e0();
  const openConversation = vJ();
  const notify = Um();
  const dismiss = $k();
  const [pending, setPending] = D.useState(false);

  async function forkToFolder(conversationId, stepIndex) {
    if (desktopWorkspaceForkPending || !conversationId || stepIndex < 0) return;
    desktopWorkspaceForkPending = true;
    setPending(true);
    let projectCreated = false;
    let projectName;
    const progressTag = "desktop-workspace-fork-progress";
    try {
      const selected = await window.dialog.showOpenDialog();
      if (!selected) return;

      notify({title: "正在派生到新工作区…", tag: progressTag, autoDismissMs: 0});
      const folderUri = Qf(selected).toString();
      const target = desktopForkFolderKey(folderUri);
      const {trajectory} = await client.getCascadeTrajectory({cascadeId: conversationId});
      if (!trajectory?.steps?.[stepIndex] || !trajectory.metadata || !target) {
        throw Error("无法确认原会话的回复切点或工作区。");
      }

      const sourceFolders = [
        ...(trajectory.metadata.workspaceUris ?? []),
        ...(trajectory.metadata.workspaces ?? []).map(workspace => workspace.workspaceFolderAbsoluteUri)
      ];
      for (const uri of sourceFolders.filter(Boolean)) {
        const source = desktopForkFolderKey(uri);
        if (source !== null && (source === target ||
            source.startsWith(target + "/") || target.startsWith(source + "/"))) {
          throw Error("请选择原工作区之外的目录，目录之间不能互相包含。");
        }
      }

      const projectId = Jf();
      projectName = selected.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "Fork";
      const folder = cc(Ih, {type: {case: "folderUri", value: folderUri}});
      await createProject(projectName, projectId, [folder], false);
      projectCreated = true;

      const cascadeId = MC();
      const result = await client.startCascade({
        cascadeId,
        source: J.CortexTrajectorySource.CASCADE_CLIENT,
        projectEnvConfig: {
          projectId,
          target: {case: "defaultProjectEnvironment", value: {}}
        },
        baseTrajectoryIdentifier: cc(yf.BaseTrajectoryIdentifierSchema, {
          identifier: {
            case: "forkFrom",
            value: {
              cascadeId: conversationId,
              stepIndex,
              referenceType: J.CortexTrajectoryReferenceType.FORK
            }
          }
        })
      });

      const roots = result?.projectEnvInfo?.workspaceUris ?? [];
      if (result?.cascadeId !== cascadeId || roots.length !== 1 ||
          desktopForkFolderKey(roots[0]) !== target) {
        throw Error("桌面后端未确认派生会话使用所选目录。");
      }
      openConversation(cascadeId, {project: projectId});
    } catch (error) {
      const projectHint = projectCreated ? ` 新项目「${projectName}」已创建，可在侧栏查看。` : "";
      notify({title: "派生到新工作区失败", message: Sn(error) + projectHint,
        tag: "desktop-workspace-fork-error"});
    } finally {
      dismiss({type: "remove", tag: progressTag});
      desktopWorkspaceForkPending = false;
      setPending(false);
    }
  }

  return {pending, forkToFolder};
}
