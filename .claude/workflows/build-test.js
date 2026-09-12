export const meta = {
  name: 'build-test',
  description: 'plan.md の独立タスクを Build(sonnet) → Test(sonnet) のパイプラインで並列処理する',
  phases: [
    { title: 'Build', detail: 'タスクごとに builder が実装して commit', model: 'sonnet' },
    { title: 'Test', detail: 'タスクごとに tester が TDD 検証', model: 'sonnet' },
  ],
}

// args: { feature: string, tasks: Array<{ id: string, title: string, files: string[] }> }
if (!args || typeof args.feature !== 'string' || !Array.isArray(args.tasks) || args.tasks.length === 0) {
  throw new Error('args は { feature, tasks: [{ id, title, files }] } の形で渡すこと')
}

const feature = args.feature
const specPath = `specs/${feature}/spec.md`
const planPath = `specs/${feature}/plan.md`

const TEST_SCHEMA = {
  type: 'object',
  properties: {
    taskId: { type: 'string' },
    passed: { type: 'boolean' },
    testsAdded: { type: 'array', items: { type: 'string' } },
    failures: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
  },
  required: ['taskId', 'passed', 'testsAdded', 'failures'],
}

const buildPrompt = (t) => [
  `feature: ${feature}`,
  `タスク ${t.id}: ${t.title}`,
  `対象ファイル: ${t.files.join(', ')}`,
  `${planPath} の ${t.id} と ${specPath} の対応する受入基準を読み、このタスクだけを実装して conventional commit する。`,
  'テストを書き換えて通すのは禁止。他タスクのファイルには触らない。',
  'ビルド/テストコマンドは CLAUDE.md の Commands セクションを参照。',
  '最後に、変更したファイル一覧と commit hash を返す。',
].join('\n')

const testPrompt = (buildResult, t) => [
  `feature: ${feature}`,
  `タスク ${t.id}: ${t.title}`,
  `builder の報告:\n${buildResult}`,
  `${specPath} でこのタスクに対応する受入基準 AC-n を特定し、TDD で検証する:`,
  '1. 期待動作を表すテストを追加（まず失敗することを確認）',
  '2. 実装がそれを通すことを確認',
  '3. 全体テストを実行して回帰がないことを確認',
  '4. 対応する AC の verify コマンドを実行',
  'ビルド/テストコマンドは CLAUDE.md の Commands セクションを参照。',
  '結果を JSON で返す。failures には失敗したテスト名と原因を書く。',
].join('\n')

log(`${feature}: ${args.tasks.length} タスクを Build → Test で処理`)

const results = await pipeline(
  args.tasks,
  (t) => agent(buildPrompt(t), {
    model: 'sonnet',
    label: `build:${t.id}`,
    phase: 'Build',
    agentType: 'builder',
    isolation: 'worktree',
  }),
  (buildResult, t) => {
    if (buildResult === null) {
      log(`${t.id}: builder が結果を返さなかったため Test をスキップ`)
      return { taskId: t.id, passed: false, testsAdded: [], failures: ['builder returned null'] }
    }
    return agent(testPrompt(buildResult, t), {
      model: 'sonnet',
      label: `test:${t.id}`,
      phase: 'Test',
      agentType: 'tester',
      schema: TEST_SCHEMA,
    })
  },
)

const summary = results.filter(Boolean)
const failed = summary.filter((r) => !r.passed)
log(`完了: ${summary.length - failed.length}/${args.tasks.length} 合格${failed.length ? `、失敗: ${failed.map((r) => r.taskId).join(', ')}` : ''}`)

return { feature, results: summary, failedTaskIds: failed.map((r) => r.taskId) }
