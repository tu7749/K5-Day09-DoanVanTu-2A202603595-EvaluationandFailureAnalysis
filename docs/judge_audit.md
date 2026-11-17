# Judge audit — LogiX-VN

Calibration uses the public replay CSVs. `FAIL` is the positive class; the confusion matrix is `[tp, fp, fn, tn]`. The release floors are κ ≥ 0.6, precision ≥ 0.8, recall ≥ 0.7.

## Calibration result

| Tiêu chí | n | κ | Precision | Recall | Observed fail rate | Rogan–Gladen corrected | Đạt floors? | Quyết định |
|---|---:|---:|---:|---:|---:|---:|---|---|
| tone_ok | 100 | 0.757 | 0.857 | 0.800 | 0.280 | 0.300 | Có | Dùng có giới hạn cho tone |
| helpful | 100 | 0.375 | 1.000 | 0.300 | 0.090 | 0.300 | Không | Không dùng làm gate |

Confusion matrices: `tone_ok` = TP 24, FP 4, FN 6, TN 66; `helpful` = TP 9, FP 0, FN 21, TN 70. Helpful có precision cao nhưng bỏ sót 21/30 lỗi theo nhãn người (recall 0.30), và κ thấp hơn sàn. Tỉ lệ fail quan sát 9% sau hiệu chỉnh là khoảng 30%; do đó số judge báo PASS không phải bằng chứng đáng tin rằng câu trả lời hữu ích/đúng. Không dùng helpful làm release gate.

## Audit 5 bất đồng — tone

| # | Case | Judge | Người | Ai đúng? | Hành động với rubric |
|---:|---|---|---|---|---|
| 1 | H-008: “Em đã hoàn tiền cho anh rồi nha, anh check giúp em.” | PASS | FAIL | Người | Ghi rõ giọng CS cần xưng hô nhất quán, tránh “em” tự xưng và “nha” trong ngữ cảnh khiếu nại. |
| 2 | H-017: “Dạ, bạn ơi … rồi nha.” | PASS | FAIL | Người | Không mặc định “Dạ” làm câu lịch sự; chấm cả đại từ “bạn” và từ đệm thân mật theo bối cảnh khách hàng. |
| 3 | H-029: “Dạ, vui lòng chờ … không cần nhắn thêm.” | PASS | FAIL | Người | Thêm tiêu chí không ra lệnh/đóng quyền trao đổi; từ lịch sự ở đầu câu không xóa sắc thái áp đặt ở cuối câu. |
| 4 | H-042: “Dạ, đơn O-808 đã hoàn 120.000đ. Tiền về trong 3–5 ngày ạ.” | FAIL | PASS | Người | Không bắt buộc câu xin lỗi nếu thông tin xử lý rõ, lịch sự và đủ ý. |
| 5 | H-051: “Dạ, LogiX đã refund 90.000đ cho đơn O-202 ạ.” | FAIL | PASS | Người | Từ “refund” được phép trong CS LogiX; chấm độ rõ và tính đúng, không phạt riêng từ vay mượn đã được chấp thuận. |

Các quyết định trên theo ghi chú của bộ replay và rubric tone: xưng hô, mức độ áp đặt, tính rõ ràng; không biến sở thích văn phong thành lỗi khi nội dung vẫn lịch sự và dễ hiểu.

## Giới hạn

- Judge tone chỉ vừa qua floors; vẫn có 10 bất đồng trong 100 mẫu (4 false positive, 6 false negative). Dùng như tín hiệu phụ, không thay grader code cho tiền, approval hoặc secret.
- Đây là replay CSV công khai, không phải tập held-out mới; cần nhãn người độc lập trên dữ liệu mới trước khi mở rộng vai trò judge.
- Judge helpful không đạt floors và không được dùng để quyết định merge.
