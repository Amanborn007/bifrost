import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Routes, Route, Navigate, useLocation, Link } from "react-router-dom";
import { Layout } from "@/components/layout";
import { ErrorBoundary } from "@/components/error-boundary";
import { DashboardPage } from "@/pages/dashboard";
import { QueryPage } from "@/pages/query";
import { CostsPage } from "@/pages/costs";
import { DomainDetailPage } from "@/pages/domain-detail";
import { KnowledgePage } from "@/pages/knowledge";
import { TasksPage } from "@/pages/tasks";
import { TaskDetailPage } from "@/pages/task-detail";
import { LearningPage } from "@/pages/learning";
import {
  CollaborationLandingPage,
  CollaborationNewPage,
  CollaborationSessionPage,
  HandoverDocDetailPage,
  HandoverDocsListPage,
} from "@/pages/collaboration";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,           // 30s — don't refetch if data is fresh
      gcTime: 5 * 60_000,          // 5min cache retention
      refetchOnWindowFocus: false, // kills the storm of refetches when user alt-tabs
      refetchOnMount: false,       // use cached data on mount
      retry: 1,                    // only retry once on failure
    },
  },
});

/**
 * Redirect legacy /entities links into the merged /knowledge page.
 * Maps ?repo_id=X (old param) → ?repo=X (new param); keeps search/domain.
 */
function EntitiesRedirect() {
  const loc = useLocation();
  const params = new URLSearchParams(loc.search);
  const legacyRepoId = params.get("repo_id");
  if (legacyRepoId && !params.get("repo")) {
    params.set("repo", legacyRepoId);
    params.delete("repo_id");
  }
  const qs = params.toString();
  return <Navigate to={`/knowledge${qs ? `?${qs}` : ""}`} replace />;
}

/**
 * Fallback route for unknown paths. Earlier the SPA rendered a blank
 * page when the URL didn't match any registered route, which made
 * typos and stale links look like an outage. The Layout wrapper keeps
 * the sidebar / chrome present so the operator can navigate out.
 */
function NotFoundPage() {
  const loc = useLocation();
  return (
    <div className="mx-auto max-w-xl px-6 py-16 text-center">
      <h1 className="text-[20px] font-semibold text-fg-0">Page not found</h1>
      <p className="mt-2 text-[14px] text-fg-2">
        Nothing routes to <code className="font-mono text-fg-1">{loc.pathname}</code>.
      </p>
      <Link
        to="/"
        className="mt-6 inline-block rounded-md border border-border bg-bg-1 px-4 py-2 text-[13px] font-medium text-fg-0 hover:bg-bg-2"
      >
        Back to overview
      </Link>
    </div>
  );
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            {/* Overview is the landing page — gives the operator
                ambient state (running extractions, recent costs,
                health) before they go pick a flow. */}
            <Route
              path="/"
              element={
                <ErrorBoundary label="Dashboard">
                  <DashboardPage />
                </ErrorBoundary>
              }
            />
            <Route
              path="/overview"
              element={
                <ErrorBoundary label="Dashboard">
                  <DashboardPage />
                </ErrorBoundary>
              }
            />
            <Route
              path="/query"
              element={
                <ErrorBoundary label="Chat">
                  <QueryPage />
                </ErrorBoundary>
              }
            />
            <Route
              path="/knowledge"
              element={
                <ErrorBoundary label="Knowledge graph">
                  <KnowledgePage />
                </ErrorBoundary>
              }
            />
            <Route
              path="/tasks"
              element={
                <ErrorBoundary label="Tasks">
                  <TasksPage />
                </ErrorBoundary>
              }
            />
            <Route
              path="/tasks/:id"
              element={
                <ErrorBoundary label="Task detail">
                  <TaskDetailPage />
                </ErrorBoundary>
              }
            />
            <Route path="/collaboration" element={<CollaborationLandingPage />} />
            <Route path="/collaboration/new" element={<CollaborationNewPage />} />
            <Route
              path="/collaboration/sessions/:id"
              element={
                <ErrorBoundary label="Collaboration session">
                  <CollaborationSessionPage />
                </ErrorBoundary>
              }
            />
            <Route
              path="/collaboration/handovers"
              element={<HandoverDocsListPage />}
            />
            <Route
              path="/collaboration/handovers/:id"
              element={
                <ErrorBoundary label="Handover detail">
                  <HandoverDocDetailPage />
                </ErrorBoundary>
              }
            />
            <Route path="/entities" element={<EntitiesRedirect />} />
            <Route
              path="/costs"
              element={
                <ErrorBoundary label="Costs">
                  <CostsPage />
                </ErrorBoundary>
              }
            />
            <Route
              path="/admin/learning"
              element={
                <ErrorBoundary label="Learning loop">
                  <LearningPage />
                </ErrorBoundary>
              }
            />
            <Route
              path="/domains/:id"
              element={
                <ErrorBoundary label="Domain detail">
                  <DomainDetailPage />
                </ErrorBoundary>
              }
            />
            {/* 404 — must be last under the Layout route so navigation
                chrome stays present and the user can recover. */}
            <Route path="*" element={<NotFoundPage />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

export default App;
