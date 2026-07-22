resource "aws_cognito_user_pool" "replika" {
  count                    = var.enable_replika_managed_auth ? 1 : 0
  name                     = "${local.name}-customers"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length    = 10
    require_lowercase = true
    require_numbers   = true
    require_symbols   = false
    require_uppercase = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_client" "replika" {
  count                                = var.enable_replika_managed_auth ? 1 : 0
  name                                 = "${local.name}-web"
  user_pool_id                         = aws_cognito_user_pool.replika[0].id
  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  callback_urls                        = ["https://${var.host_name}/oauth2/idpresponse"]
  logout_urls                          = ["https://${var.host_name}/login"]
  supported_identity_providers         = ["COGNITO"]
  prevent_user_existence_errors        = "ENABLED"
}

resource "aws_cognito_user_pool_domain" "replika" {
  count        = var.enable_replika_managed_auth ? 1 : 0
  domain       = "${substr(replace(local.name, "_", "-"), 0, 30)}-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.replika[0].id
}

resource "aws_cognito_user_pool_ui_customization" "replika" {
  count        = var.enable_replika_managed_auth ? 1 : 0
  user_pool_id = aws_cognito_user_pool.replika[0].id
  client_id    = "ALL"
  css          = file("${path.module}/cognito-login.css")
  image_file   = filebase64("${path.module}/../../tower/static/img/clodexa-logo.png")

  depends_on = [aws_cognito_user_pool_domain.replika]
}
