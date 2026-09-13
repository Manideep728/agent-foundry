#pragma once

namespace afrunner {

// Realtime. Orders runs, and is never subtracted.
//
// Two clocks, because NTP steps the realtime clock and a VM guest resynchronises
// after the host sleeps, which can move it by hours. A duration derived from it
// can come out negative or absurd (decision D12).
long long unix_ms();

// Monotonic. Only ever subtracted.
long long monotonic_ms();

}  // namespace afrunner
