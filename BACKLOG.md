# Technical Debt Backlog

- [ ] Refactor tab-table logic: Abstract redundant "Intercept-Render-Trigger" code into a single helper function.
    
- [ ] Replace `data_editor` version-bumping hack with native row-click event handling (when API allows).
    
- [ ] Optimize state management: Replace `st.cache_data.clear()` with targeted `st.session_state` updates to prevent full-app reloads on checkbox toggles.
