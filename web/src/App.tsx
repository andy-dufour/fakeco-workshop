import { Navigate, Route, Routes, useLocation } from "react-router";
import { homeFor, useAuth } from "./auth";
import Layout from "./components/Layout";
import SignIn from "./pages/SignIn";
import Dashboard from "./pages/customer/Dashboard";
import Trades from "./pages/customer/Trades";
import Transfers from "./pages/customer/Transfers";
import Tickets from "./pages/customer/Tickets";
import Chat from "./pages/customer/Chat";
import SupportCustomers from "./pages/support/Customers";
import SupportCustomer from "./pages/support/Customer";
import SupportConversations from "./pages/support/Conversations";
import Transcript from "./pages/support/Transcript";
import SupportTickets from "./pages/support/Tickets";
import OpsOverview from "./pages/ops/Overview";
import OpsStories from "./pages/ops/Stories";
import OpsFaults from "./pages/ops/Faults";
import OpsLoadgen from "./pages/ops/Loadgen";
import OpsRuns from "./pages/ops/Runs";
import OpsTickets from "./pages/ops/Tickets";
import Holds from "./pages/compliance/Holds";
import RestrictedList from "./pages/compliance/Restricted";
import type { Role } from "./types";
import type { ReactNode } from "react";

function Guard({ role, children }: { role: Role; children: ReactNode }) {
  const { user } = useAuth();
  const loc = useLocation();
  if (!user) return <Navigate to="/signin" replace state={{ from: loc.pathname }} />;
  if (user.role !== role) return <Navigate to={homeFor[user.role]} replace />;
  return <>{children}</>;
}

export default function App() {
  const { user } = useAuth();
  const g = (role: Role, el: ReactNode) => <Guard role={role}>{el}</Guard>;
  return (
    <Routes>
      <Route path="/signin" element={<SignIn />} />
      <Route element={user ? <Layout /> : <Navigate to="/signin" replace />}>
        <Route index element={g("customer", <Dashboard />)} />
        <Route path="chat" element={g("customer", <Chat />)} />
        <Route path="trades" element={g("customer", <Trades />)} />
        <Route path="transfers" element={g("customer", <Transfers />)} />
        <Route path="tickets" element={g("customer", <Tickets />)} />

        <Route path="support" element={g("support", <SupportCustomers />)} />
        <Route path="support/customers/:id" element={g("support", <SupportCustomer />)} />
        <Route path="support/conversations" element={g("support", <SupportConversations />)} />
        <Route path="support/conversations/:id" element={g("support", <Transcript />)} />
        <Route path="support/tickets" element={g("support", <SupportTickets />)} />

        <Route path="ops" element={g("operations", <OpsOverview />)} />
        <Route path="ops/stories" element={g("operations", <OpsStories />)} />
        <Route path="ops/faults" element={g("operations", <OpsFaults />)} />
        <Route path="ops/loadgen" element={g("operations", <OpsLoadgen />)} />
        <Route path="ops/runs" element={g("operations", <OpsRuns />)} />
        <Route path="ops/tickets" element={g("operations", <OpsTickets />)} />

        <Route path="compliance" element={g("compliance", <Holds />)} />
        <Route path="compliance/restricted" element={g("compliance", <RestrictedList />)} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
