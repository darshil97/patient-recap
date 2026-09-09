import { Route, Routes } from "react-router-dom";
import { Shell } from "./components/Shell";
import { StatsProvider } from "./hooks/StatsContext";
import { TodayPage } from "./pages/TodayPage";
import { SchedulePage } from "./pages/SchedulePage";
import { PatientsPage } from "./pages/PatientsPage";
import { PatientDetailPage } from "./pages/PatientDetailPage";
import { VisitPage } from "./pages/VisitPage";
import { NotFoundPage } from "./pages/NotFoundPage";

function App() {
  return (
    <StatsProvider>
      <Routes>
        <Route element={<Shell />}>
          <Route path="/" element={<TodayPage />} />
          <Route path="/follow-ups" element={<TodayPage />} />
          <Route path="/schedule" element={<SchedulePage />} />
          <Route path="/patients" element={<PatientsPage />} />
          <Route path="/patients/:patientId" element={<PatientDetailPage />} />
          <Route path="/visits/:visitId" element={<VisitPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </StatsProvider>
  );
}

export default App;
