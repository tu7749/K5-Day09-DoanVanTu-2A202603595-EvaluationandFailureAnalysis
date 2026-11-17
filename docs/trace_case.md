# Trace case: FM-05 / FM-08 on C-014

Reproduce from `src/`:

```bash
python -m lab9.trace_case --shape seeded_regression --claim C-014 --seed 11
python -m lab9.trace_case --shape fixed --claim C-014 --seed 11
```

| Trường | Nội dung |
|---|---|
| Trial | `seeded_regression C-014@11`; run ID `R-seeded_regression-C-014-s11` (report `E-seeded_regression-20261117-5c86`) |
| Triệu chứng | `proposal_mismatch`; quote entitlement 150.000đ, prior refund 30.000đ, ledger chỉ ghi 90.000đ; reply lại nói 120.000đ. Nhãn yêu cầu ledger 120.000đ. |
| Bước sai đầu tiên | Event 5 `proposal`: `{ "amount_vnd": 120000 }`; agent tự trừ 30.000đ trước khi gọi host. |
| Vì sao là bước đầu tiên | Event 1–3 tạo handoff, đọc đúng order và quote 150.000đ. Event 4 đọc đúng lịch sử 30.000đ. Sai ở event 5 vì host `issue_refund` tự áp trần theo đơn và trừ khoản trước đó lần nữa. |
| Failure mode | FM-05 `pre_deducted`; hệ quả FM-08 vì reply 120.000đ khác ledger 90.000đ. |
| Đối chứng | `fixed C-014@11` giữ quote 150.000đ; host trừ 30.000đ và ghi 120.000đ; reply và ledger khớp. Lab 8 grader PASS. |
| Regression test | `test_fm08_regression_on_seeded_regression_and_fixed` trong `tests/test_lab9_graders.py`: FM-08 và `RefundAmountCorrect` fail trên regression, pass trên fixed. |
| Chưa chắc chắn | Case này chứng minh đường đi của lỗi khi có prior refund; không đo tỉ lệ lỗi trên người dùng thật hay model khác MockLLM. |

Đọc ledger cùng reply: `ReplyAmountMatchesLedger` so tiền trong event `reply` với ledger; `RefundAmountCorrect` so ledger với nhãn riêng. Cả hai grader đều dùng code, không dùng judge.
