from django import forms


class RegisterForm(forms.Form):
    nickname = forms.CharField(label="Nickname", max_length=64)
    email = forms.EmailField(label="E-Mail")

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class EmailForm(forms.Form):
    email = forms.EmailField(label="E-Mail")

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class AdminLoginForm(forms.Form):
    email = forms.EmailField(label="E-Mail")
    password = forms.CharField(label="Passwort", widget=forms.PasswordInput)
    otp_token = forms.CharField(
        label="Einmalcode (TOTP)",
        max_length=8,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}),
    )

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class DeleteAccountForm(forms.Form):
    confirm = forms.BooleanField(label="Ja, meinen Account endgültig löschen", required=True)
