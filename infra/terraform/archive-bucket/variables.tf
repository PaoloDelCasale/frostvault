variable "aws_region" {
  description = "AWS region for the dedicated archive bucket."
  type        = string
  default     = "eu-south-1"
}

variable "bucket_name" {
  description = "Globally unique S3 bucket name for this installation."
  type        = string
}

variable "abort_multipart_upload_days" {
  description = "Days after initiation before incomplete multipart uploads are aborted."
  type        = number
  default     = 7
}

variable "tags" {
  description = "Optional tags applied to the bucket."
  type        = map(string)
  default     = {}
}
